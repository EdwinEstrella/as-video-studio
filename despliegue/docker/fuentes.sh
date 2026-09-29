#!/bin/sh
# Las fuentes del estudio, montadas al construir la imagen. Es la misma logica
# que `fuentes()` de instalar.sh, y por la misma razon:
#
# el estudio MIDE el texto con Python (PIL) y lo DIBUJA con el navegador. Si no
# son el mismo fichero, la cuenta sale bien y el numero mal: los rotulos se
# salen de su caja y no da ningun error, solo se ve mirando un PNG. Por eso aqui
# se fuerza que el fichero que mide y el que dibuja sean el mismo.
#
# Verdana, Georgia y Arial son las de Microsoft (paquete oficial de Debian, que
# las baja con su licencia: NO viajan en el repositorio). Si la descarga falla
# se usan las libres de DejaVu -- otra letra, pero cuadrada, que es lo que
# importa -- y se dice en voz alta. Con EXIGIR_FUENTES_MS=1 el build se para en
# vez de seguir con el repuesto.
set -eu

CARPETA="${ESTUDIO_FUENTES:-/usr/local/share/fonts/estudio}"
CONF=/etc/fonts/conf.d/61-as-video-studio.conf

echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true | debconf-set-selections
apt-get install -y --no-install-recommends ttf-mscorefonts-installer >/dev/null 2>&1 \
  || echo "AVISO: no se han podido bajar las fuentes de Microsoft (suele ser el servidor de descarga)."
apt-get install -y --no-install-recommends fonts-dejavu-core >/dev/null

if ! fc-list | grep -qi verdana; then
  if [ "${EXIGIR_FUENTES_MS:-0}" = "1" ]; then
    echo "ERROR: sin Verdana y EXIGIR_FUENTES_MS=1: el build se para." >&2
    exit 1
  fi
  echo "AVISO: Verdana no esta; se usara DejaVu. Los textos se veran con otra letra pero cuadrados."
fi

mkdir -p "$CARPETA"

# Enlaza el primer candidato que exista con el nombre que espera el codigo.
enlaza() {
  destino="$1"; shift
  encontrado=""
  for candidato in "$@"; do
    encontrado="$(find /usr/share/fonts /usr/local/share/fonts -iname "$candidato" -type f 2>/dev/null | head -1)"
    [ -n "$encontrado" ] && break
  done
  [ -n "$encontrado" ] || return 1
  ln -sf "$encontrado" "$CARPETA/$destino"
}

# Las seis que dibujan de verdad: sin ninguna, el build se para.
obligada() {
  destino="$1"; shift
  enlaza "$destino" "$@" || { echo "ERROR: ninguna fuente para $destino." >&2; exit 1; }
}
obligada verdana.ttf  Verdana.ttf      DejaVuSans.ttf
obligada verdanab.ttf Verdana_Bold.ttf DejaVuSans-Bold.ttf
obligada georgia.ttf  Georgia.ttf      DejaVuSerif.ttf
obligada georgiab.ttf Georgia_Bold.ttf DejaVuSerif-Bold.ttf
obligada arial.ttf    Arial.ttf        DejaVuSans.ttf
obligada arialbd.ttf  Arial_Bold.ttf   DejaVuSans-Bold.ttf
# Microsoft no publica estas para la web: van con repuesto libre SIEMPRE. El
# estudio solo las nombra en su tabla de fuentes.
enlaza tahoma.ttf   Tahoma.ttf Verdana.ttf DejaVuSans.ttf                || true
enlaza tahomabd.ttf Tahoma_Bold.ttf Verdana_Bold.ttf DejaVuSans-Bold.ttf || true
enlaza consola.ttf  Consolas.ttf DejaVuSansMono.ttf                      || true
enlaza consolab.ttf Consolas_Bold.ttf DejaVuSansMono-Bold.ttf            || true
enlaza segoeui.ttf  Segoe_UI.ttf DejaVuSans.ttf                          || true
enlaza segoeuib.ttf Segoe_UI_Bold.ttf DejaVuSans-Bold.ttf                || true

# El alias que hace que el NAVEGADOR resuelva un nombre al MISMO fichero que abre
# PIL. Calculado de lo que haya quedado enlazado y no escrito a mano: a mano, el
# dia que cambia un enlace los dos lados se separan sin avisar.
printf '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n' > "$CONF"
for par in "Verdana:verdana.ttf" "Georgia:georgia.ttf" "Arial:arial.ttf" \
           "Tahoma:tahoma.ttf" "Consolas:consola.ttf" "Segoe UI:segoeui.ttf"; do
  familia="${par%%:*}"; fichero="${par##*:}"
  real="$(readlink -f "$CARPETA/$fichero" 2>/dev/null || true)"
  [ -n "$real" ] || continue
  nombre="$(fc-query -f '%{family[0]}' "$real" 2>/dev/null || true)"
  [ -n "$nombre" ] || continue
  [ "$nombre" = "$familia" ] && continue      # ya es esa: no hace falta alias
  cat >> "$CONF" <<XML
  <match target="pattern">
    <test qual="any" name="family"><string>$familia</string></test>
    <edit name="family" mode="assign" binding="same"><string>$nombre</string></edit>
  </match>
XML
done
printf '</fontconfig>\n' >> "$CONF"
fc-cache -f >/dev/null 2>&1

# La comprobacion de verdad: que «Verdana» se dibuje con el fichero que se mide.
mide="$(readlink -f "$CARPETA/verdana.ttf")"
dibuja="$(readlink -f "$(fc-match Verdana -f '%{file}')" 2>/dev/null || true)"
if [ "$mide" = "$dibuja" ]; then
  echo "OK: mide y dibuja el mismo fichero ($(basename "$mide"))"
else
  echo "AVISO: Verdana se dibuja con $(basename "${dibuja:-nada}") y se mide con $(basename "$mide")."
  [ "${EXIGIR_FUENTES_MS:-0}" = "1" ] && exit 1
fi
exit 0
