# El acceso (Node). Contexto: la raiz del repositorio.
#
# Dos etapas: better-sqlite3 lleva un binario nativo. Normalmente se baja ya
# compilado, pero si no hay uno para la plataforma se compila, y para eso hacen
# falta herramientas de C que no tienen nada que hacer en la imagen final.
FROM node:22-bookworm-slim AS librerias
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 make g++ \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /login
COPY despliegue/login/package.json ./
RUN npm install --omit=dev --no-audit --no-fund

FROM node:22-bookworm-slim
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000 \
    DATA_DIR=/login-datos
WORKDIR /login
COPY --from=librerias /login/node_modules ./node_modules
COPY despliegue/login/ ./
COPY despliegue/docker/estudio-clave-docker /usr/local/bin/estudio-clave
# Solo escribe su carpeta de datos (sesiones y hashes), y no como root. Es el
# usuario `node` (UID 1000) que ya trae la imagen.
RUN sed -i 's/\r$//' /usr/local/bin/estudio-clave \
 && chmod 755 /usr/local/bin/estudio-clave \
 && mkdir -p /login-datos && chown node:node /login-datos
USER node
EXPOSE 3000
CMD ["node", "server.js"]
