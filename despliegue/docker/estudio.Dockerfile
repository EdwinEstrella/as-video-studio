# El estudio (Python + ffmpeg + navegador + CLIs de Claude y de Antigravity). Contexto: la raiz del
# repositorio. Reproduce lo que instalar.sh monta en un VPS; ver LEEME.md.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ARG EXIGIR_FUENTES_MS=0
# El CLI de Antigravity (agy) es opcional: con AGY_OBLIGATORIO=1 el build falla si
# no se puede instalar; con 0 (lo normal) la imagen se construye igual y solo el
# motor de imagen de Google queda sin binario.
ARG AGY_OBLIGATORIO=0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Chromium de Debian y no Edge/Chrome de sus repositorios: sale del mismo apt, no
# depende de claves externas y existe tambien para arm64. `contrib` es donde vive
# el instalador de las fuentes de Microsoft.
RUN sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget cabextract fontconfig \
      ffmpeg chromium \
 && rm -rf /var/lib/apt/lists/*

# Node solo para el CLI de Claude. Se copia de la imagen oficial en vez de bajar
# el script de NodeSource: una dependencia de red menos en el build.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install -g @anthropic-ai/claude-code --no-audit --no-fund \
 && claude --version

# El CLI de Antigravity (agy), el motor de imagen de Google. El instalador lo deja
# en ~/.local/bin del usuario que corre el build (root), y AHI NO SIRVE: en
# ejecucion HOME es /datos/home, que es un volumen y tapa lo que hubiera dentro.
# Por eso se copia a /usr/local/bin, que es de la imagen, y se le dice al motor
# donde esta con ESTUDIO_AGY. La sesion de cada cuenta NO va en la imagen: vive en
# /datos/secretos/agy/<id>/ y se hace desde la pantalla.
RUN if curl -fsSL https://antigravity.google/cli/install.sh | bash \
      && [ -x /root/.local/bin/agy ]; then \
        install -m 755 /root/.local/bin/agy /usr/local/bin/agy \
        && rm -rf /root/.local /root/.gemini \
        && agy --version; \
    elif [ "${AGY_OBLIGATORIO}" = "1" ]; then \
        echo "no se pudo instalar agy (AGY_OBLIGATORIO=1)" >&2; exit 1; \
    else \
        echo "AVISO: agy no se ha instalado; el motor de imagen de Google no funcionara" >&2; \
    fi

# Las fuentes: ver fuentes.sh (es el punto delicado del estudio).
ENV ESTUDIO_FUENTES=/usr/local/share/fonts/estudio
COPY despliegue/docker/fuentes.sh /tmp/fuentes.sh
RUN sed -i 's/\r$//' /tmp/fuentes.sh \
 && apt-get update \
 && EXIGIR_FUENTES_MS=${EXIGIR_FUENTES_MS} sh /tmp/fuentes.sh \
 && rm -rf /var/lib/apt/lists/* /tmp/fuentes.sh

# El envoltorio del navegador (--no-sandbox y --disable-dev-shm-usage; el porque
# esta en despliegue/estudio-edge). Es el mismo fichero que usa instalar.sh.
COPY despliegue/estudio-edge /usr/local/bin/estudio-edge
RUN sed -i 's/\r$//; s|__NAVEGADOR__|/usr/bin/chromium|' /usr/local/bin/estudio-edge \
 && chmod 755 /usr/local/bin/estudio-edge

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# UID 1000 con HOME real y escribible: el navegador muere con «cannot create
# directory» si HOME no existe o no es suyo, y no dice que le falta HOME. HOME
# vive DENTRO del volumen de datos para que la sesion del CLI de Claude
# (~/.claude y ~/.claude.json) sobreviva a recrear el contenedor. /datos se crea
# aqui con su dueno: un volumen nuevo hereda ese dueno al montarse por primera vez.
RUN mkdir -p /datos/home \
 && useradd --uid 1000 --home-dir /datos/home --shell /usr/sbin/nologin studio \
 && chown -R studio:studio /datos

COPY . /app

# reglas.json y tarifas.json: la imagen lleva la version por defecto como semilla
# y el codigo las lee de /datos (ver estudio-entrada.sh). reglas.py escribe en su
# ruta fija junto al modulo, asi que ahi va un enlace y no un fichero.
RUN mkdir -p /opt/semilla \
 && mv /app/motores/reglas/reglas.json /opt/semilla/reglas.json \
 && cp /app/tarifas.json /opt/semilla/tarifas.json \
 && ln -s /datos/reglas.json /app/motores/reglas/reglas.json \
 && sed -i 's/\r$//' /app/despliegue/docker/estudio-entrada.sh \
 && install -m 755 /app/despliegue/docker/estudio-entrada.sh /usr/local/bin/estudio-entrada \
 && mkdir -p /app/cache && chown studio:studio /app/cache

# TODA ruta de datos, en /datos y a un sitio FIJO: los proyectos guardan rutas
# absolutas que entran en la firma de cada paso. Cambiar /datos deja obsoleto
# todo lo ya pagado. Es el equivalente de datos/entorno de instalar.sh.
ENV HOME=/datos/home \
    ESTUDIO_PROYECTOS=/datos/proyectos \
    ESTUDIO_PRESETS=/datos/presets.json \
    ESTUDIO_BANCO=/datos/banco \
    ESTUDIO_BANCO_PRESETS=/datos/banco/presets \
    ESTUDIO_SECRETOS=/datos/secretos \
    ESTUDIO_ENV=/datos/secretos/.env \
    ESTUDIO_RECETAS=/datos/recetas.json \
    ESTUDIO_AJUSTES=/datos/ajustes.json \
    ESTUDIO_TARIFAS=/datos/tarifas.json \
    ESTUDIO_ESTADISTICAS=/datos/estadisticas.json \
    ESTUDIO_COSTE_GLOBAL=/datos/coste_global.jsonl \
    ESTUDIO_BITACORA_GLOBAL=/datos/bitacora_global.jsonl \
    ESTUDIO_MOTORES=/app/motores \
    ESTUDIO_AGY=/usr/local/bin/agy \
    ESTUDIO_EDGE=/usr/local/bin/estudio-edge

USER studio
EXPOSE 8110
ENTRYPOINT ["estudio-entrada"]
CMD ["python", "app.py", "--puerto", "8110", "--host", "0.0.0.0"]
