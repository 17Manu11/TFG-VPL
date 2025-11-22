#!/bin/bash
# vpl_evaluate.sh — ejecuta BIOTES si está, sino fallback (multi-lenguaje), y envía a la API
set -Eeuo pipefail

API_URL="${API_URL:-http://150.214.214.19:8000}"
TIMEOUT=40

# 0) Selección de archivo de código del alumno
CODE_FILE="${CODE_FILE:-}"
if [ -z "$CODE_FILE" ]; then
  for pattern in "*.py" "*.java" "*.cpp" "*.c" "*.js" "*.ts"; do
    f=$(find . -maxdepth 1 -type f -name "$pattern" | head -n1 || true)
    [ -n "${f:-}" ] && CODE_FILE="$f" && break
  done
fi
if [ -z "$CODE_FILE" ] || [ ! -f "$CODE_FILE" ]; then
  echo "❌ No se encontró archivo de código. Define CODE_FILE o sube el archivo."
  exit 0
fi


# 0.1) Construir comando de ejecución según la extensión
RUN_CMD=""
case "$CODE_FILE" in
  *.py)
    RUN_CMD="python3 \"$CODE_FILE\""
    ;;

  *.java)
    if ! javac -encoding UTF-8 "$CODE_FILE" 2>/tmp/compile.err; then
      echo "❌ Error al compilar Java:"
      cat /tmp/compile.err
      exit 0
    fi
    CLASSNAME="$(basename "$CODE_FILE" .java)"
    RUN_CMD="java \"$CLASSNAME\""
    ;;

  *.c)
    gcc "$CODE_FILE" -o main 2>/tmp/compile.err || {
      echo "❌ Error al compilar C:"
      cat /tmp/compile.err
      exit 0
    }
    RUN_CMD="./main"
    ;;

  *.cpp)
    g++ "$CODE_FILE" -o main 2>/tmp/compile.err || {
      echo "❌ Error al compilar C++:"
      cat /tmp/compile.err
      exit 0
    }
    RUN_CMD="./main"
    ;;

  *)
    echo "❌ Extensión de archivo no soportada: $CODE_FILE"
    exit 0
    ;;
esac




# 0.2) Crear wrapper ./vpl_test para que BIOTES pueda ejecutar el programa
if [ -n "${RUN_CMD:-}" ]; then
  printf '#!/bin/bash\nset -Eeuo pipefail\nexec bash -lc %q\n' "$RUN_CMD" > vpl_test
  chmod +x vpl_test
fi

# 1) Archivos de contexto (mantener estos mensajes)
CASOS_FILE="${CASOS_FILE:-vpl_evaluate.cases}"
ENUN_FILE="${ENUN_FILE:-enunciado.txt}"
BASE_FILE="${BASE_FILE:-codigo_base.txt}"
INST_FILE="${INST_FILE:-restricciones.txt}"



# 2) BIOTES (si existe el tester oficial de VPL)
if [ -f "vpl_evaluate.cases" ] && [ -f "vpl_evaluate.cpp" ]; then

  # (1) Cargar entorno VPL si existe (silencioso)
  set +u +e
  [ -f "./vpl_environment.sh" ] && . ./vpl_environment.sh || true
  : "${PROFILE_RUNNED:=0}"
  [ -f "./common_script.sh" ] && . ./common_script.sh || true
  set -e -u

  # (2) Normalizar .cases (silencioso)
  if command -v dos2unix >/dev/null 2>&1; then
    dos2unix -q vpl_evaluate.cases || true
  else
    perl -pe 's/\r$//' -i vpl_evaluate.cases 2>/dev/null || true
  fi
  tail -c1 vpl_evaluate.cases | od -An -t u1 | grep -q '10' || printf '\n' >> vpl_evaluate.cases
  awk '
    BEGIN{FS="=";OFS="="}
    /^[ \t]*(case|input|output)[ \t]*=/ { gsub(/[ \t]+/, "", $1); sub(/^[ \t]+/, "", $2); print $1, $2; next }
    {print}
  ' vpl_evaluate.cases > .cases_sanitized
  cp -f .cases_sanitized evaluate.cases

  # (3) Compilar tester (silencioso)
  g++ -O0 -std=c++17 -lm -o .vpl_tester vpl_evaluate.cpp 2>biotes_compile.err || true

  if [ -x ./.vpl_tester ]; then
    # (4) Ejecutar tester (silencioso, capturando log)
    try_run() {
      "$@" 2>&1 | tee biotes_output.txt >/dev/null || true
      [ -s biotes_output.txt ] && ! grep -qi "No test case found" biotes_output.txt
    }
    if ! try_run ./.vpl_tester; then
      if ! try_run ./.vpl_tester "evaluate.cases"; then
        if ! try_run bash -lc "cat evaluate.cases | ./.vpl_tester"; then
          CASES_DATA="$(cat evaluate.cases)"
          if ! try_run env VPL_SUBFILE0="evaluate.cases" ./.vpl_tester; then
            if ! try_run env VPL_SUBFILE0_CONTENT="$CASES_DATA" ./.vpl_tester; then
              try_run env VPL_TESTCASES="$CASES_DATA" ./.vpl_tester || true
            fi
          fi
        fi
      fi
    fi
  fi
fi

# 3) Fallback runner (si BIOTES no produjo casos) — multi-lenguaje con RUN_CMD
NECESITA_FALLBACK=0
if [ ! -s biotes_output.txt ] || grep -qi "No test case found" biotes_output.txt; then
  NECESITA_FALLBACK=1
fi

if [ "$NECESITA_FALLBACK" -eq 1 ] && [ -f "$CASOS_FILE" ] && [ -n "$RUN_CMD" ]; then
  RUN_CMD="$RUN_CMD" python3 - "$CASOS_FILE" <<'PY'
import json, sys, subprocess, os, re
casos_path = sys.argv[1]
run_cmd = os.environ.get("RUN_CMD","").strip()
if not run_cmd: sys.exit(0)

raw = open(casos_path, 'r', encoding='utf-8', errors='ignore').read().splitlines()
cases=[]; cur={}; mode=None
for line in raw:
    s=line.strip()
    if not s: continue
    lower=s.lower()
    if lower.startswith("case"):
        if cur: cases.append(cur)
        cur={"id": line.split("=",1)[1].strip(), "input":"", "expected":""}; mode=None
    elif lower.startswith("input"):
        mode="input"; tail=line.split("=",1)[1].strip() if "=" in line else ""
        if tail: cur["input"] += tail + "\n"
    elif lower.startswith("output"):
        mode="output"; tail=line.split("=",1)[1].strip() if "=" in line else ""
        if tail: cur["expected"] += tail + "\n"
    else:
        if mode=="input":   cur["input"]   += line + "\n"
        elif mode=="output":cur["expected"]+= line + "\n"
if cur: cases.append(cur)

def norm(t:str)->str:
    t=t.replace("\r\n","\n").replace("\r","\n")
    t=re.sub(r"[ \t]+"," ",t)
    return "\n".join(x.strip() for x in t.splitlines()).strip()

def find_num(txt:str):
    m=re.search(r"Resultado:\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", txt)
    if m:
        try: return float(m.group(1))
        except: return None
    return None

def compare(expected:str, output:str):
    outN = norm(output)
    exp_num = find_num(expected)
    if exp_num is not None:
        out_num = find_num(output)
        if out_num is None: return False, "no_Resultado"
        tol = max(1e-6, 1e-6*abs(exp_num))
        if abs(out_num-exp_num) > tol:
            return False, f"Resultado difiere: esp={exp_num} obt={out_num}"
    for ln in [l for l in expected.splitlines() if l.strip()]:
        if ln.strip().startswith("Resultado:"): continue
        if norm(ln) not in outN:
            return False, f"falta_linea:{ln.strip()}"
    return True, ""

results=[]; passed=0
for c in cases:
    try:
        proc = subprocess.run(["bash","-lc", run_cmd],
                              input=c["input"], capture_output=True, text=True, timeout=8)
        out=(proc.stdout or "") + (("\n"+proc.stderr) if proc.stderr else "")
        ok, why = compare(c["expected"], out)
    except Exception as e:
        out, ok, why = str(e), False, "exception"
    passed += int(ok)
    results.append({
        "id": c["id"], "ok": bool(ok), "reason": (None if ok else why),
        "input": c["input"].strip(), "expected": c["expected"].strip(),
        "output": out.strip()
    })

json.dump({"total": len(cases), "passed": passed, "cases": results},
          open('biotes_output.json','w'), ensure_ascii=False, indent=2)
PY
fi

# 4) Construir el JSON para la API (lectura segura)
BODY=$(python3 - "$CODE_FILE" "$ENUN_FILE" "$BASE_FILE" "$CASOS_FILE" "$INST_FILE" << 'PY'
import json,sys,os
code_path,enun_path,base_path,casos_path,inst_path = sys.argv[1:6]
def read_or_empty(p):
    try:
        return open(p,'r',encoding='utf-8',errors='ignore').read() if os.path.isfile(p) else ""
    except Exception:
        return ""
def read_or_empty_base(p):
    if os.path.isfile(p):
        return open(p,'r',encoding='utf-8',errors='ignore').read()
    for alt in ["codigo-base.txt","base.txt","base_codigo.txt","baseCode.txt"]:
        if os.path.isfile(alt):
            return open(alt,'r',encoding='utf-8',errors='ignore').read()
    return ""
def read_or_empty_restr(p):
    if os.path.isfile(p):
        return open(p,'r',encoding='utf-8',errors='ignore').read()
    if os.path.isfile("instrucciones.txt"):
        return open("instrucciones.txt",'r',encoding='utf-8',errors='ignore').read()
    return ""
res=""
if os.path.isfile("biotes_output.json"):
    res = read_or_empty("biotes_output.json")
elif os.path.isfile("biotes_output.txt"):
    res = read_or_empty("biotes_output.txt")
payload={
  "codigo":        read_or_empty(code_path),
  "enunciado":     read_or_empty(enun_path),
  "codigo_base":   read_or_empty_base(base_path),
  "casos_prueba":  read_or_empty(casos_path),
  "restricciones": read_or_empty_restr(inst_path),
  "resultados_casos": res
}
print(json.dumps(payload, ensure_ascii=False))
PY
)

# 5) POST a la API + salida amigable
HTTP_CODE=$(curl -sS -m "$TIMEOUT" -o feedback.json -w '%{http_code}' \
  -H "Content-Type: application/json" \
  -X POST "$API_URL/retroalimentacion" \
  --data-binary "$BODY" || echo "000")

echo "===== CORRECCIÓN ====="
if [ "$HTTP_CODE" != "200" ]; then
  echo "⚠️  Error al pedir feedback. HTTP=$HTTP_CODE"
  [ -s feedback.json ] && cat feedback.json || true
else
  python3 - << 'PY' || true
import json, sys
data=json.load(open('feedback.json','r',encoding='utf-8',errors='ignore'))
print(data.get("retroalimentacion","(sin texto)"))
print("NOTA_TESTS:", data.get("nota_tests","-"))
print("NOTA:", data.get("nota","-"))
PY
fi

# 6) Salir sin bloquear la evaluación nativa de VPL
exit 0
