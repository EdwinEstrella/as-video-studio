#!/bin/sh
# Prepara /datos antes de arrancar el estudio. Corre en CADA arranque y no toca
# nada que ya exista: los datos son del usuario, la imagen se reemplaza.
set -eu

D=/datos
mkdir -p "$D/proyectos" "$D/banco/presets" "$D/secretos" "$HOME"
# Las claves de API y las sesiones del CLI de Claude viven aqui dentro.
chmod 700 "$D/secretos"

# reglas.json es el unico fichero del arbol de codigo que la aplicacion
# REESCRIBE (el destilador aprende de tu feedback). En la imagen es un enlace a
# /datos/reglas.json; se siembra la primera vez y nunca mas, o cada actualizacion
# de la imagen le borraria al usuario lo que ha aprendido su estudio.
[ -f "$D/reglas.json" ] || cp /opt/semilla/reglas.json "$D/reglas.json"

# Las tarifas son del producto, pero se copian a los datos para poder ajustarlas
# sin que una actualizacion se las lleve (igual que hace instalar.sh).
[ -f "$D/tarifas.json" ] || cp /opt/semilla/tarifas.json "$D/tarifas.json"

exec "$@"
