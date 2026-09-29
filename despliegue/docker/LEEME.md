# AS Video Studio en Dokploy (Docker)

Es lo mismo que monta `instalar.sh` en un VPS, empaquetado en un
`docker-compose.yml` en la raíz del repositorio. El reparto no cambia (ver
[../LEEME.md](../LEEME.md)): el estudio no sabe lo que es un usuario y el acceso
va delante.

```
Traefik de Dokploy (dominio + HTTPS de Let's Encrypt), configurado con etiquetas
├── /login /api/{csrf,login,logout,me,health} /css/ /js/ /assets/  →  login   (Node :3000)
└── todo lo demás → forwardAuth (login :3000/api/_auth, en cada petición)
                    → si hay sesión  →  estudio (Python :8110)
                    → si no la hay   →  302 a /login
```

No hay nginx: Traefik hace su papel. El `forwardAuth` es el `auth_request`, y
el 302 a `/login` lo contesta el propio acceso (`/api/_auth?redirigir=1`),
porque Traefik devuelve al navegador la respuesta del acceso tal cual, sin el
`error_page` con el que nginx convertía el 401.

## Desplegar

1. En Dokploy: **Create Service → Compose**, origen Git con este repositorio,
   tipo *Docker Compose*, ruta `./docker-compose.yml`.
2. Pestaña **Environment**:

   | Variable | Obligatoria | Qué es |
   |---|---|---|
   | `DOMINIO` | sí | el nombre público, sin esquema (`estudio.midominio.com`) |
   | `SESSION_SECRET` | sí | firma las sesiones, mínimo 32 caracteres: `openssl rand -hex 48`. Si cambia, se cierran todas |
   | `ESTUDIO_LOTES` | no (4) | planos que se renderizan a la vez. Poner el número de hilos de la máquina, máx. 16 (medido: en 8 hilos, 4 → 6,75 fps y 8 → 10,40 fps) |
   | `APP_ORIGENES` | no | lista de orígenes admitidos, separados por comas. Por defecto `https://$DOMINIO`. Si sirves también `www.` u otro nombre, añádelo aquí o el login dirá «Origen no permitido» |
   | `SECURE_COOKIES` | no (true) | solo bajarla a `false` para probar sin certificado |
   | `HOSTINGER_VPS_ID` | no | solo para el enlace de «he olvidado la contraseña» |
   | `EXIGIR_FUENTES_MS` | no (0) | a `1`, el build se para si no se pueden bajar las fuentes de Microsoft (ver más abajo) |

3. Pestaña **Domains**: **déjala vacía**. El enrutado va en las etiquetas del
   compose, porque esa pestaña crea un router por dominio y no deja colgarle el
   `forwardAuth`. Un dominio puesto ahí además crearía un router SIN acceso por
   delante hacia el servicio que elijas: el estudio abierto a internet. El DNS
   de `DOMINIO` tiene que apuntar ya al servidor, o Let's Encrypt no da el
   certificado. Ningún servicio publica puertos en el host.
4. **Deploy.** El primer build tarda: instala Chromium, ffmpeg, Node y el CLI de
   Claude.

## La primera cuenta

Como en n8n: la primera vez que se abre el dominio, sin ninguna cuenta creada,
la pantalla de acceso pide **crear la cuenta** con un correo y una contraseña
(mínimo 8 caracteres). Desde entonces se entra con esos dos datos, y esa
pantalla no vuelve a salir.

**Hasta que se crea, la crea quien llegue primero.** Abre el dominio justo
después del primer despliegue. Si alguien se adelantara, se ve en los logs del
servicio `login` (`[alta] primera cuenta creada: <correo>`): se borra el volumen
`login_datos` y se vuelve a desplegar.

Desde la terminal del contenedor `login` sigue funcionando el CLI, para
cambiar o recuperar una contraseña:

```bash
estudio-clave --nueva          # pone una contraseña nueva (Intro: la genera)
estudio-clave                  # vuelve a enseñar la guardada
```

Una cuenta creada así se llama `estudio` y no tiene correo: se entra
escribiendo `estudio` en el campo del correo.

## La sesión de Claude

Entra en el estudio y sigue la guía de inicio: pide la cuenta de Claude (es un
login del CLI, no una clave de API). La sesión queda en el volumen de datos
(`HOME=/datos/home` para la sesión por defecto, `/datos/secretos` para las
cuentas de respaldo), así que **sobrevive a recrear el contenedor y a
actualizar**. Las claves de API se guardan en `/datos/secretos`.

## Los datos, y la ruta que no se toca

Dos volúmenes con nombre:

| Volumen | Punto de montaje | Qué guarda |
|---|---|---|
| `estudio_datos` | `/datos` | proyectos, estilos (`presets.json`), banco, `secretos/` (claves y sesiones del CLI), ajustes, recetas, tarifas, estadísticas, gasto, bitácora, `reglas.json` y el `HOME` (`/datos/home`) |
| `login_datos` | `/login-datos` | sesiones, hashes de contraseña y la copia de recuperación |

**`/datos` no se cambia nunca.** Un proyecto guarda rutas absolutas (las salidas
de cada unidad, los manifiestos de cada versión y las referencias de estilo) y
entran en la firma de cada paso. Mover el punto de montaje deja `assets`,
`callouts` y `render` en obsoleto, y la pantalla ofrece regenerar todos los
planos ya pagados. Si necesitas cambiar de sitio, sigue lo de «Copiar un proyecto
a otra máquina» de `CLAUDE.md`.

Cada variable `ESTUDIO_*` apunta ya dentro de `/datos` desde el Dockerfile
(`despliegue/docker/estudio.Dockerfile`); es el `datos/entorno` de `instalar.sh`.
`ESTUDIO_SIMULAR=1` es solo para las pruebas (no sale a ninguna API de pago) y
no se pone aquí.

**`reglas.json`.** Es el único fichero del código que la aplicación reescribe.
En la imagen `motores/reglas/reglas.json` es un enlace a `/datos/reglas.json`, y
al primer arranque `estudio-entrada` siembra ese fichero con el de la imagen. Una
actualización no lo pisa. (Se hace con un enlace porque `reglas.py` tiene su
ruta fija junto al módulo y no lee ninguna variable de entorno.) `tarifas.json`
se trata igual, como en `instalar.sh`.

**Proyectos que vienen de otra máquina.** No basta con copiar la carpeta: hay
que pasar antes `herramientas/mudar_proyecto.py` para mudar las rutas y re-sellar
las firmas, y comprobar en las dos máquinas cuántos ficheros producidos hay de
verdad en disco. Sin eso, todo lo ya pagado sale obsoleto. Detalle en
`CLAUDE.md`, sección «Copiar un proyecto o un estilo a otra máquina».

## Lo que hay dentro, y por qué

- **Navegador: `chromium` de Debian**, llamado por `estudio-edge` con
  `--no-sandbox --disable-dev-shm-usage` (`ESTUDIO_EDGE`). Sale del apt de la
  imagen, sin repositorios externos ni claves. Además el servicio lleva
  `shm_size: 1gb`: los 64 MB de Docker no aguantan una tanda larga.
- **Fuentes**: `fuentes.sh` repite la lógica de `instalar.sh`. Verdana, Georgia
  y Arial de Microsoft (`ttf-mscorefonts-installer`, se bajan al construir, no
  viajan en el repositorio) o DejaVu de repuesto, enlazadas con los nombres que
  espera el código y con el alias de fontconfig calculado de lo que quedó
  enlazado, para que PIL mida con el fichero con el que el navegador dibuja. Si
  la descarga falla el build lo dice y sigue con DejaVu (textos con otra letra,
  pero cuadrados); con `EXIGIR_FUENTES_MS=1` se para.
- **Usuario sin privilegios** (UID 1000) con `HOME` real y escribible.
  El estudio escribe además en `/app/cache` (miniaturas), que existe y es suyo.
- **Traefik en vez de nginx** (`despliegue/nginx-sitio.conf`), con lo mismo:
  el acceso gana a los routers del estudio por prioridad, `/api/login` lleva un
  `ratelimit` de 12 por minuto con ráfaga de 5, el estudio va comprimido y el
  HTTP redirige a HTTPS. `X-Forwarded-Proto` y la IP real los pone Traefik, y el
  acceso confía en un salto (`TRUST_PROXY=1`): sin eso la cookie `secure` se
  rompe y el limitador junta a todo el mundo.
- **Los nombres llevan `asvs-`** (routers, middlewares y el alias `asvs-login`
  de la red `dokploy-network`): esa red y ese Traefik son de toda la máquina, y
  un `login` a secas podría resolver al servicio de otro proyecto.
- **Las subidas grandes.** Traefik no limita el tamaño del cuerpo (nginx
  necesitaba `client_max_body_size 2g`), pero en Traefik v3 el entrypoint corta
  la LECTURA de una petición a los 60 s (`respondingTimeouts.readTimeout`). Si
  subir una referencia de estilo grande falla, en Dokploy → Settings → Traefik
  (`traefik.yml`) se sube en el entrypoint `websecure`:

  ```yaml
  entryPoints:
    websecure:
      transport:
        respondingTimeouts:
          readTimeout: 3600s
  ```

  Ese plazo es solo para leer la petición; la respuesta no tiene límite en
  Traefik v3 (`writeTimeout` es 0 por defecto), así que las esperas largas no
  necesitan nada.

## Actualizar

Redeploy en Dokploy. Se reconstruye la imagen y se recrean los contenedores; los
volúmenes no se tocan, ni la sesión de Claude, ni `reglas.json`.

## Si algo falla

- **Se entra y se vuelve a la pantalla de acceso**: la cookie no llega como
  `secure`. Comprueba que se entra por `https://` y que `SECURE_COOKIES` sigue
  en `true`.
- **Se ve el estudio sin pedir contraseña**: hay un dominio en la pestaña
  Domains de Dokploy. Quítalo (ver «Desplegar», paso 3).
- **404 de Traefik o certificado por defecto**: `DOMINIO` no coincide con el
  nombre por el que entras, o el DNS todavía no apunta al servidor.
- **«Origen no permitido»**: falta el nombre en `APP_ORIGENES`.
- **Los rótulos se salen de su caja**: mira el log del build, la línea de
  `fuentes.sh` («mide y dibuja el mismo fichero» o el aviso).
- **El render se queda sin memoria**: baja `ESTUDIO_LOTES`.
