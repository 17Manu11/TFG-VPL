from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests, os, json, re
from typing import Optional, Tuple

app = FastAPI()

# -------- CORS --------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# -------- OpenRouter --------
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-nemo")
OPENROUTER_HTTP_REFERRER = os.getenv("OPENROUTER_HTTP_REFERRER")
OPENROUTER_APP_TITLE     = os.getenv("OPENROUTER_APP_TITLE", "VPL LLM Feedback UMA")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
# Un poco más de margen para aumentar la extensión de la retroalimentación
MAX_TOKENS  = int(os.getenv("MAX_TOKENS", "1600"))
TIMEOUT     = int(os.getenv("TIMEOUT", "60"))

# -------- Límites de recorte --------
EVIDENCIA_MAX_CHARS = int(os.getenv("EVIDENCIA_MAX_CHARS", "6000"))
CASOS_MAX_CHARS     = int(os.getenv("CASOS_MAX_CHARS", "4000"))
# Soporta ambas variables de entorno por compatibilidad
RESTR_MAX_CHARS     = int(os.getenv("RESTRICCIONES_MAX_CHARS", os.getenv("INSTRUCCIONES_MAX_CHARS", "3000")))

def _clip(txt: str, limit: int) -> str:
    if not txt: return ""
    if len(txt) <= limit: return txt
    head = txt[: limit // 2].rstrip()
    tail = txt[-(limit // 2) :].lstrip()
    return f"{head}\n[... {len(txt)-limit} chars omitidos ...]\n{tail}"

def _maybe_pretty(obj) -> str:
    """Si es JSON, lo indenta; si no, devuelve str."""
    try:
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, ensure_ascii=False, indent=2)
        if isinstance(obj, str):
            parsed = json.loads(obj)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return str(obj or "")

# --- Parser robusto del texto de BIOTES ---
def parse_biotes_text_counts(text: str) -> Optional[Tuple[int, int]]:
    """
    Extrae (passed, total) desde el log textual de BIOTES:
      1) última línea "Testing N/M : ..."
      2) bloque "<|--\n-Failed tests ... \n--|>"
      3) bloque "Summary of tests ... X tests run/ Y test(s) passed"
    """
    if not text or not isinstance(text, str):
        return None

    total = None
    for m in re.finditer(r'Testing\s+\d+\s*/\s*(\d+)\s*:', text):
        try:
            total = int(m.group(1))
        except Exception:
            pass

    failed = None
    m = re.search(r'<\|\--\s*-Failed tests(.*?)--\|>', text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        block = m.group(1)
        failed = len(re.findall(r'^\s*Test\s+\d+', block, flags=re.IGNORECASE | re.MULTILINE))

    if total is not None and failed is not None:
        passed = max(0, total - failed)
        return passed, total

    m = re.search(
        r'Summary of tests.*?(\d+)\s*tests?\s*run.*?(\d+)\s*tests?\s*passed',
        text, flags=re.IGNORECASE | re.DOTALL
    )
    if m:
        try:
            total2 = int(m.group(1))
            passed2 = int(m.group(2))
            return passed2, total2
        except Exception:
            pass

    return None

# --- Nota por evidencia (BIOTES/JSON) ---
def grade_from_evidence(evidencia) -> Optional[Tuple[int, int, int]]:
    """
    Devuelve (nota, passed, total). Admite:
      - JSON: {"passed":X,"total":Y} o {"cases":[{"ok":bool,...},...]}
      - Log BIOTES: parser específico
      - Último recurso: patrón 'X/Y'
    """
    if not evidencia:
        return None

    # 1) JSON directo
    try:
        data = json.loads(evidencia) if isinstance(evidencia, str) else evidencia
        if isinstance(data, dict):
            if "passed" in data and "total" in data:
                p, t = int(data["passed"]), int(data["total"])
                if t > 0 and 0 <= p <= t:
                    nota = 10 if p == t else int(round(10 * (p / t)))
                    return max(0, min(10, nota)), p, t
            if isinstance(data.get("cases"), list):
                lst = data["cases"]
                t = len(lst)
                p = sum(1 for c in lst if isinstance(c, dict) and c.get("ok") is True)
                if t > 0:
                    nota = 10 if p == t else int(round(10 * (p / t)))
                    return max(0, min(10, nota)), p, t
    except Exception:
        pass

    # 2) Texto de BIOTES
    if isinstance(evidencia, str):
        if ("Testing" in evidencia and "Failed tests" in evidencia) or ("Summary of tests" in evidencia):
            ct = parse_biotes_text_counts(evidencia)
            if ct:
                p, t = ct
                nota = 10 if p == t else int(round(10 * (p / t)))
                return max(0, min(10, nota)), p, t

        # 3) Último recurso: 'X/Y'
        xy = re.findall(r'(\d+)\s*/\s*(\d+)', evidencia)
        for a, b in reversed(xy):
            a, b = int(a), int(b)
            if b > 0 and 0 <= a <= b:
                nota = 10 if a == b else int(round(10 * (a / b)))
                return max(0, min(10, nota)), a, b

    return None

def build_cases_summary(evidencia, max_len=2000) -> str:
    """Si evidencia es JSON con 'cases', lista ✔/✘; si es texto, devuelve un recorte."""
    if not evidencia:
        return ""
    try:
        data = json.loads(evidencia) if isinstance(evidencia, str) else evidencia
        if isinstance(data, dict) and isinstance(data.get("cases"), list):
            lst = data["cases"]
            total = len(lst)
            passed = sum(1 for c in lst if isinstance(c, dict) and c.get("ok") is True)
            lines = [f"Resumen de casos (servidor): {passed}/{total} pasan"]
            for c in lst:
                if not isinstance(c, dict): continue
                cid = str(c.get("id", "caso"))
                ok  = bool(c.get("ok"))
                lines.append(f"- {cid}: {'✔' if ok else '✘'}")
                if not ok:
                    exp = c.get("expected"); out = c.get("output")
                    if exp is not None: lines.append(f"    esperado: {str(exp).strip()[:300]}")
                    if out is not None: lines.append(f"    obtenido: {str(out).strip()[:300]}")
            return "\n".join(lines)[:max_len]
    except Exception:
        pass
    return _clip(str(evidencia), max_len)

# ==========================================================
#                    ENDPOINT PRINCIPAL
# ==========================================================
@app.post("/retroalimentacion")
async def obtener_retroalimentacion(request: Request):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise HTTPException(status_code=500, detail="Falta OPENROUTER_API_KEY en variables de entorno")

    # 1) Body
    try:
        datos = await request.json()
    except Exception:
        raw = await request.body()
        raise HTTPException(status_code=400, detail={
            "error": "JSON inválido",
            "raw_sample": raw.decode("utf-8", "ignore")[:200]
        })

    codigo_alumno   = datos.get("codigo", "")
    enunciado       = datos.get("enunciado", "")
    codigo_base     = datos.get("codigo_base", "")
    casos_prueba    = datos.get("casos_prueba", "")

    # Cambiamos a RESTRICCIONES (aceptamos 'restricciones' o 'instrucciones' por compat)
    restricciones   = datos.get("restricciones")
    if restricciones is None:
        restricciones = datos.get("instrucciones", "")

    # Alias: resultados_casos ≡ biotes_log
    resultados_casos = datos.get("resultados_casos") or datos.get("biotes_log")

    # 2) Preparar bloques (con recortes bonitos)
    casos_txt       = _clip(_maybe_pretty(casos_prueba),     CASOS_MAX_CHARS)
    evidencia_txt   = _clip(_maybe_pretty(resultados_casos), EVIDENCIA_MAX_CHARS)
    restr_txt       = _clip(str(restricciones or ""),        RESTR_MAX_CHARS)
    hay_restr       = bool(restr_txt.strip())

    # Normalizar RESTRICCIONES en lista R1..Rn (ignorando encabezados)
    restr_norm_lines = []
    for ln in restr_txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower().strip().rstrip(":")
        if low in ("obligaciones", "recomendaciones"):
            continue
        restr_norm_lines.append(s.strip(" \t-*•"))
    restr_norm_block = "\n".join(f"- [R{i+1}] {ln}" for i, ln in enumerate(restr_norm_lines)) if restr_norm_lines else ""

    # 3) Calcular nota por casos (preferente)
    nota_calc = None
    resumen_calc = ""
    g = grade_from_evidence(resultados_casos)
    if g:
        nota_calc, passed, total = g
        resumen_calc = f"{passed}/{total} casos (nota por casos = {nota_calc}/10)"

    # 4) PROMPT estilo simple (como el anterior)
    prompt_usuario = f"""
DATOS
ENUNCIADO:
{enunciado}

CÓDIGO BASE:
{codigo_base}

CÓDIGO DEL ALUMNO:
{codigo_alumno}

PRESENCIA_DE_CODIGO_BASE: {"Sí" if (codigo_base or "").strip() else "No"}
PRESENCIA_DE_RESTRICCIONES: {"Sí" if hay_restr else "No"}
"""

    if casos_txt:
        prompt_usuario += f"""

CASOS DE PRUEBA (texto):
{casos_txt}
"""

    if resultados_casos:
        prompt_usuario += f"""

RESULTADOS CASOS DE PRUEBA (log/JSON):
{evidencia_txt}
"""

    # Bloque de RESTRICCIONES (raw + normalizada)
    if hay_restr:
        prompt_usuario += f"""

RESTRICCIONES — LEE ESTAS NORMAS PARA EVALUAR
1) Considera OBLIGATORIAS únicamente las que estén bajo el encabezado literal
   'Obligaciones:' en el texto siguiente.
2) Todo lo bajo 'Recomendaciones:' NO es obligatorio; úsalo solo como consejo.
3) Si el texto NO contiene encabezados, asume que TODO lo que sigue es OBLIGATORIO.
4) Extrae cada restricción como bullet si empieza por '*', '-', o '•'.

=== TEXTO DE RESTRICCIONES (tal cual) ===
{restr_txt}
=== FIN TEXTO ===

LISTA NORMALIZADA (para referenciar R1..Rn):
{restr_norm_block if restr_norm_block else '(no bullets detectados)'}
"""
    else:
        prompt_usuario += """

RESTRICCIONES:
(no aportadas)
"""

    if resumen_calc:
        prompt_usuario += f"""

RESUMEN SERVIDOR (no citar nota):
{resumen_calc}
"""

    # 5) Mensaje de sistema claro y con más extensión objetivo
    system_msg = (
        "Eres corrector de ejercicios de programación. Habla al alumno (tú) con claridad.\n"
        "Líneas ≤ 90 caracteres y sin dobles saltos. Extensión objetivo: 14–24 líneas.\n\n"
        "Casos (si hay evidencia): DI qué casos pasan/fallan y por qué. Si NO hay evidencia,\n"
        "no inventes resultados: di que no hay datos de ejecución.\n\n"
        "RESTRICCIONES: usa SOLO el sub-bloque 'Obligaciones:' como checklist estricto. El\n"
        "sub-bloque 'Recomendaciones:' NO es obligatorio y solo aporta sugerencias. Si no hay\n"
        "encabezados, trata todo el texto como Obligaciones. Enumera R1..Rn siguiendo el orden\n"
        "de bullets y marca: 'Cumple' / 'No cumple' / 'No verificable' + evidencia breve.\n"
        "Sé conservador: si no lo ves en el código, 'No verificable'. Citas útiles: 'import math',\n"
        "comprensiones '[x for x in y]', 'break/return' en bucles, múltiples 'return', E/S exacta.\n\n"
        "Ajuste a código base/contratos: si 'CÓDIGO BASE' está vacío, indícalo. Si existe,\n"
        "comprueba firmas/nombres/contratos y cita faltas concretas.\n\n"
        "Formato de salida (en este orden):\n"
        "Corrección\n"
        "- Resumen (1–2 líneas). Si hay evidencia, añade 'Casos: X/Y'. Si no hay, dilo.\n"
        "- Cumplimiento del enunciado (concreto y verificable).\n"
        "- Ajuste al código base/contratos (cita faltas si las hay).\n"
        "- Complejidad/eficiencia y posibles errores.\n"
        "- Análisis de casos: 'casoN: ✔/✘ - motivo' (si no hay evidencia, omite esta sección).\n"
        "- Chequeo de RESTRICCIONES OBLIGATORIAS: lista R1..Rn con Cumple/No cumple/\n"
        "  No verificable + motivo breve por cada una. NO mezcles Recomendaciones aquí.\n"
        "- Consejos de mejora (bullets accionables). Apóyate en Recomendaciones cuando aplique.\n"
        "- Buenas prácticas detectadas.\n"
        "- Próximos pasos (2–3 bullets).\n\n"
        "REGLAS PARA NOTA_IA Y CIERRE FINAL\n"
        "0) TEXTO PLANO: la nota final debe ir en una línea normal, sin Markdown, sin '\\',\n"
        "sin código y sin formato especial.\n"
        "1) NOTA_IA (0–10), SIN usar tests: valora enunciado, cumplimiento de RESTRICCIONES,\n"
        "   ajuste al código base/contratos y legibilidad/estilo. Cualquier 'No cumple' debe\n"
        "   penalizar. Da una única cifra y una justificación breve (≤140 caracteres). No\n"
        "   menciones tests NI escribas ninguna línea de 'NOTA_TESTS'.\n"
        "2) Cierre obligatorio (ÚLTIMA línea, exactamente esta y nada más debajo):\n"
        "   NOTA_IA: <0-10> - <justificación≤140c>\n"
    )

    # 6) Llamada a OpenRouter
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if OPENROUTER_HTTP_REFERRER: headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERRER
    if OPENROUTER_APP_TITLE:     headers["X-Title"]       = OPENROUTER_APP_TITLE

    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": prompt_usuario}
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }

    try:
        r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error de red al contactar OpenRouter: {e}")

    if r.status_code != 200:
        try: detalle = r.json()
        except Exception: detalle = r.text
        raise HTTPException(status_code=r.status_code, detail={"openrouter_error": detalle})

    j = r.json()
    if not (isinstance(j, dict) and "choices" in j and j["choices"]):
        raise HTTPException(status_code=502, detail=f"Respuesta inesperada del modelo: {j}")

    contenido = j["choices"][0]["message"]["content"].strip()
    retroalimentacion = contenido

    # --- PURGA de cualquier 'NOTA_TESTS:' que el modelo haya incluido ---
    retroalimentacion = "\n".join(
        ln for ln in retroalimentacion.splitlines()
        if not ln.strip().startswith("NOTA_TESTS:")
    ).strip()

    # --- EXTRAER SOLO NOTA_IA del cierre obligatorio ---
    nota_ia_num = None
    nota_ia_comentario = ""
    tail_lines = retroalimentacion.strip().splitlines()
    ia_line = next((ln for ln in reversed(tail_lines) if ln.strip().startswith("NOTA_IA:")), None)
    if ia_line:
        m = re.match(r'^NOTA_IA\s*:\s*(10|[0-9])\s*-\s*(.+)$', ia_line.strip())
        if m:
            nota_ia_num = int(m.group(1))
            nota_ia_comentario = m.group(2).strip()

    # NOTA de tests (servidor)
    # (Deriva de BIOTES / fallback; si no hay, None)
    nota_tests_num = int(nota_calc) if nota_calc is not None else None

    # --- CALCULAR LA MEDIA QUE DEVUELVE LA API ---
    if (nota_tests_num is not None) and (nota_ia_num is not None):
        nota_final = str(int(round((nota_tests_num + nota_ia_num) / 2)))
    elif nota_tests_num is not None:
        nota_final = str(nota_tests_num)
    elif nota_ia_num is not None:
        nota_final = str(nota_ia_num)
    else:
        nota_final = "No proporcionada"

    # 7) Adjuntar resumen de casos ANTES de la única línea final NOTA_IA
    cases_block = build_cases_summary(resultados_casos)
    if cases_block:
        lines = retroalimentacion.splitlines()
        # Insertar justo antes de la ÚLTIMA línea "NOTA_IA:"
        idx_ia = next((i for i in range(len(lines)-1, -1, -1)
                       if lines[i].strip().startswith("NOTA_IA:")), None)
        if idx_ia is not None:
            head = "\n".join(lines[:idx_ia]).rstrip()
            tail = "\n".join(lines[idx_ia:]).rstrip()
            retroalimentacion = (
                f"{head}\n\n--- Resumen de casos (servidor) ---\n{cases_block}\n\n{tail}"
            ).strip()
        else:
            retroalimentacion = f"{retroalimentacion}\n\n--- Resumen de casos (servidor) ---\n{cases_block}"

    # La nota que devolvemos es la media calculada
    return {
        "retroalimentacion": retroalimentacion,
        "nota": nota_final,  # media IA/tests si ambas existen
        "nota_tests": (str(nota_tests_num) if nota_tests_num is not None else "No disponible"),
        "nota_ia": (str(nota_ia_num) if nota_ia_num is not None else "No disponible"),
        "nota_ia_comentario": nota_ia_comentario,
    }

# -------- Health --------
@app.get("/health")
async def health():
    return {"ok": True, "backend": "openrouter", "model": OPENROUTER_MODEL}
