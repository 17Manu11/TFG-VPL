# TFG-VPL
Aquí se encuentran todos los archivos que se utilizan en el sistema de retroalimentación automática en VPL. Estos se van a dividir en dos tipos, los archivos para VPL y por otro lado la API. Dentro de los archivos de VPL, todos se deben de colocar en ficheros para la ejecución, codigo_base.txt, enunciado.txt, restricciones.txt y vpl_evaluate.cases se deben rellenar con la información específica de cada ejercicio. los demás (vpl_evaluate.sh, vpl_evaluate.cpp y la ApiOpenRouter.py) contienen el código que hace funcionar el sistema, en vpl_evaluate.sh hay que poner la IP pública que vas a utilizar.

Los archivos que son propios de cada ejercicio, los voy a dejar rellenos con un ejercicio muy simple. La solución que cumple todo la dejo por aquí (Hay que quitar las comillas):

"# Leer valores de x y y

x = float(input("x:"))
y = float(input("y:"))"

"# Calcular la expresión

resultado = (-4 + 6 * (y ** 0.5)) / (2 * x + x**3)

"Mostrar resultado

print("Resultado:", resultado)

