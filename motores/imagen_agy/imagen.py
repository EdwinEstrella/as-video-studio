"""
Motor de imagen sobre Antigravity CLI (`agy`).

Usa la herramienta interna `generate_image` de agy en modo no interactivo
(`-p`). Lo que devuelve se recorta al aspecto del lienzo y se escala al tamano
canonico del estudio (1536x1024 apaisado, 1024x1024 cuadrado, 1024x1536
vertical).

Mismo contrato que `imagen_openai`: `generar(prompt, referencias, quality=,
tamano=)` -> (png, meta), y `normalizar(ruta, cache)`. El resto de pasos no
tiene que saber con que motor se dibuja.

QUE ES UNA CUENTA AQUI
----------------------
Como en el CLI de Claude, no es una clave: es un LOGIN. Cada cuenta vive en su
propio HOME aislado (`<secretos>/agy/<id>/`), que se le pasa a agy por entorno
(HOME, USERPROFILE, APPDATA y XDG_*) JUNTO CON `SSH_CONNECTION`: sin esa variable
agy usa el llavero del sistema, que es UNO para todos los HOME, y no habria
aislamiento (medido en Windows; ver `entorno_de`). Sin ninguna cuenta
configurada se usa la sesion por defecto de agy --la del llavero, sin
SSH_CONNECTION--, que es como iba antes de que hubiera cuentas.

Este motor NO importa codigo del Estudio (ver motores/README.md): lo que necesita
lo lee por CONTRATO.

  - `<secretos>/claves.json`  -> `agy.cuentas: [{id, etiqueta, home, entrada,
                                  activa}]`. Lo escribe la pantalla.
  - `<secretos>/salud_agy.json` -> como respondio cada cuenta la ultima vez.
                                  Lo escribe ESTE motor en cada llamada real, y
                                  la pantalla lo lee: preguntar «¿tiene sesion?»
                                  lanzando agy gastaria cupo cada vez que se abre
                                  Configuracion.

EL REPARTO ENTRE CUENTAS (modelado en spigot/accountPool.ts)
------------------------------------------------------------
Se usa la primera cuenta sana en el orden del usuario. Cuando una falla, se
aparta segun el motivo y la imagen se reintenta con la siguiente:

    capacidad / sobrecarga    45 s
    error del servidor        20 s
    cupo agotado              5 h, o lo que diga el mensaje (`reset after 4h59m`)
    sesion caducada           fuera hasta que se vuelva a entrar

Con todas apartadas se espera a la que vuelva antes, con un tope
(`ESTUDIO_AGY_ESPERA_MAX`, 10 min): pasado eso la imagen falla con un mensaje
claro en vez de colgar la tanda. El apartado se guarda en disco, asi que un cupo
agotado el martes sigue agotado tras reiniciar el servicio.

La concurrencia contra agy es modesta a proposito (`ESTUDIO_AGY_PARALELO`, 2 por
defecto): cada llamada arranca un proceso y gasta cupo de suscripcion.
"""
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from PIL import Image

TAMANOS = {"apaisado": "1536x1024", "cuadrado": "1024x1024", "vertical": "1024x1536"}
DIMENSIONES = {
    "apaisado": (1536, 1024),
    "cuadrado": (1024, 1024),
    "vertical": (1024, 1536),
}
ASPECT_RATIOS = {
    "apaisado": "3:2",
    "cuadrado": "1:1",
    "vertical": "2:3",
}

#: Cuantas referencias admite la herramienta de imagen. NO esta medido contra
#: agy: es el tope documentado de los modelos de imagen de Gemini (14). Si se
#: pasa, se recorta por el FINAL de la lista y se deja dicho en `meta`
#: (`refs_omitidas`), nunca en silencio. Se puede mover con ESTUDIO_AGY_MAX_REFS.
MAX_REFERENCIAS = 14

DEFECTO = "__defecto__"

#: Cuanto se aparta una cuenta segun por que fallo (segundos).
ESPERA_CAPACIDAD_S = 45
ESPERA_SERVIDOR_S = 20
ESPERA_CUPO_S = 5 * 3600

SIN_VENTANA = {}
if os.name == "nt":
    SIN_VENTANA["creationflags"] = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )

_LOCK = threading.RLock()


# ------------------------------------------------------------------ errores

class ErrorAgy(RuntimeError):
    """Un fallo de agy que el Estudio puede ensenar tal cual."""
    tipo = "otro"
    cuenta = ""
    espera_s = None


class SesionCaducada(ErrorAgy):
    tipo = "sesion"


class CupoAgotado(ErrorAgy):
    tipo = "cupo"


class Saturado(ErrorAgy):
    tipo = "capacidad"


class ErrorServidor(ErrorAgy):
    tipo = "servidor"


class TiempoAgotado(ErrorAgy):
    tipo = "tiempo"


class SinCuentas(ErrorAgy):
    tipo = "sin_cuentas"


_CLASES = {"sesion": SesionCaducada, "cupo": CupoAgotado, "capacidad": Saturado,
           "servidor": ErrorServidor, "tiempo": TiempoAgotado, "otro": ErrorAgy}


# ------------------------------------------------------------------ rutas

def ruta_agy():
    """El ejecutable de agy. UNICO sitio que lo busca: el login lo pide aqui.

    Orden: la variable ESTUDIO_AGY (una instalacion en el servidor, o un doble
    en las pruebas), el PATH y los sitios donde lo deja el instalador.
    """
    fijada = (os.environ.get("ESTUDIO_AGY") or "").strip()
    if fijada:
        return fijada
    en_path = shutil.which("agy")
    if en_path:
        return en_path
    candidatos = []
    if os.name == "nt":
        candidatos.append(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                       "agy", "bin", "agy.exe"))
    else:
        candidatos += ["/usr/local/bin/agy", os.path.expanduser("~/.local/bin/agy")]
    for candidato in candidatos:
        if os.path.exists(candidato):
            return candidato
    return "agy"


def instalado():
    """Si hay un agy que lanzar. Solo mira el disco: no ejecuta nada."""
    ruta = ruta_agy()
    return bool(shutil.which(ruta) or os.path.exists(ruta))


def carpeta_secretos():
    """Se mira el entorno EN CADA LLAMADA: las suites lo redirigen tras importar."""
    return os.environ.get("ESTUDIO_SECRETOS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "secretos")


def ruta_claves():
    return os.path.join(carpeta_secretos(), "claves.json")


def ruta_salud():
    return os.path.join(carpeta_secretos(), "salud_agy.json")


def _leer_json(ruta):
    try:
        with open(ruta, "r", encoding="utf-8-sig") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


# ------------------------------------------------------------------ cuentas

def cuentas_configuradas():
    """Las cuentas escritas en el almacen, EN ORDEN. -> [dict]"""
    crudas = (_leer_json(ruta_claves()).get("agy") or {})
    crudas = crudas.get("cuentas") if isinstance(crudas, dict) else crudas
    salida = []
    for cruda in crudas or []:
        if not isinstance(cruda, dict):
            continue
        cid = str(cruda.get("id") or "").strip()
        if not cid:
            continue
        salida.append({"id": cid,
                       "etiqueta": str(cruda.get("etiqueta") or "").strip(),
                       "home": str(cruda.get("home") or "").strip(),
                       "entrada": bool(cruda.get("entrada")),
                       "activa": cruda.get("activa") is not False})
    return salida


def cuenta_por_defecto():
    return {"id": DEFECTO, "etiqueta": "sesión por defecto de agy", "home": "",
            "entrada": True, "activa": True}


def cuentas_usables():
    """Las que se pueden llamar: activas y con el acceso hecho. -> [dict]

    Sin NINGUNA cuenta configurada se usa la sesion por defecto de agy. Con
    cuentas configuradas pero ninguna lista NO se cae a la por defecto: seria
    gastar la sesion de otra persona sin que nadie lo haya pedido.
    """
    todas = cuentas_configuradas()
    if not todas:
        return [cuenta_por_defecto()]
    return [c for c in todas if c["activa"] and c["entrada"]]


def nombre_de(cuenta):
    return cuenta.get("etiqueta") or (
        "la sesión por defecto de agy" if cuenta.get("id") == DEFECTO
        else cuenta.get("id") or "una cuenta")


def entorno_de(cuenta):
    """El entorno con el que se lanza agy para esa cuenta.

    MEDIDO en Windows, y es lo que decide todo lo demas:

      - agy guarda el login en el llavero del SISTEMA (Credential Manager). Con
        un HOME vacio y sin mas, agy CONTESTA: el llavero es el mismo para todos
        los HOME, asi que cambiar de HOME no separa cuentas.
      - con `SSH_CONNECTION` puesto agy no usa el llavero (una sesion remota no
        lo tiene) y guarda/lee el login en ficheros bajo HOME. Ahi si: con un HOME
        vacio pide entrar de nuevo, y cada HOME es una cuenta. Ademas fuerza el
        flujo «enlace + codigo» en vez de abrir un navegador EN EL SERVIDOR.
      - y a la inversa: la sesion por defecto de la persona (la del llavero) NO
        funciona con SSH_CONNECTION -- pide login --, asi que la cuenta por
        defecto se lanza SIN ella (y sin heredarla del entorno del servicio).

    Por eso las cuentas del Estudio (con `home`) van SIEMPRE con SSH_CONNECTION,
    tanto al entrar como al generar, y la sesion por defecto nunca.
    BROWSER=none es el cinturon en los dos casos.
    """
    entorno = dict(os.environ)
    entorno["BROWSER"] = "none"
    home = str((cuenta or {}).get("home") or "").strip()
    if not home:
        entorno.pop("SSH_CONNECTION", None)
        return entorno
    entorno["SSH_CONNECTION"] = "127.0.0.1 50000 127.0.0.1 22"
    home = os.path.abspath(home)
    os.makedirs(home, exist_ok=True)
    entorno["HOME"] = home
    entorno["USERPROFILE"] = home
    if os.name == "nt":
        entorno["APPDATA"] = os.path.join(home, "AppData", "Roaming")
    entorno["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    entorno["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
    entorno["XDG_STATE_HOME"] = os.path.join(home, ".local", "state")
    entorno["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    return entorno


# ------------------------------------------------------------------ salud

def _reloj():
    return time.time()


def _dormir(segundos):
    time.sleep(segundos)


def salud_leer():
    return _leer_json(ruta_salud())


def _escribir_salud(datos):
    ruta = ruta_salud()
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = "%s.%d.%d.tmp" % (ruta, os.getpid(), threading.get_ident() & 0xffff)
    with open(temporal, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=2)
    os.replace(temporal, ruta)


def salud_de(cuenta_id):
    """La ficha de esa cuenta, o None si nunca se le ha hablado."""
    ficha = salud_leer().get(cuenta_id)
    return dict(ficha) if isinstance(ficha, dict) else None


def anotar(cuenta_id, estado, mensaje="", hasta=None, para=""):
    """Apunta como respondio esa cuenta. -> la ficha escrita

    estado: ok | cupo | sesion | capacidad | tiempo | error. `hasta` es el
    instante (epoch) hasta el que la cuenta se deja fuera del reparto.
    """
    ahora = _reloj()
    ficha = {"estado": estado,
             "mensaje": " ".join(str(mensaje or "").split())[:400],
             "para": str(para or "")[:120],
             "cuando": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ahora)),
             "epoch": int(ahora),
             "hasta": int(hasta) if hasta else None}
    with _LOCK:
        datos = salud_leer()
        datos[cuenta_id] = ficha
        _escribir_salud(datos)
    return dict(ficha)


def olvidar(cuenta_id):
    """Se borra lo apuntado (al entrar de nuevo, salir o quitar la cuenta)."""
    with _LOCK:
        datos = salud_leer()
        habia = datos.pop(cuenta_id, None) is not None
        if habia:
            _escribir_salud(datos)
    return habia


def bloqueo_de(ficha, ahora=None):
    """Por que una cuenta no se puede usar ahora: None | ('sesion',) | ('espera', hasta)"""
    if not isinstance(ficha, dict):
        return None
    if ficha.get("estado") == "sesion":
        return ("sesion",)
    hasta = ficha.get("hasta")
    if hasta and hasta > (ahora if ahora is not None else _reloj()):
        return ("espera", hasta)
    return None


# ------------------------------------------------------------------ clasificar

_DURACION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)(?![a-z])", re.I)
_RENUEVA = re.compile(
    r"(?:reset(?:s)?\s+(?:after|in)|try again in|retry in|retry after|"
    r"available again in)\s*[:=]?\s*([0-9hmsd. ]{2,24})", re.I)


def parse_duracion(texto):
    """'4h59m' -> 17940.0, '17940s' -> 17940.0, '5m30s' -> 330.0. None si no hay."""
    texto = str(texto or "").strip()
    if not texto:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", texto):
        return float(texto)
    factores = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
    total, visto = 0.0, False
    for numero, unidad in _DURACION.findall(texto):
        total += float(numero) * factores[unidad.lower()]
        visto = True
    return total if visto and total > 0 else None


def espera_del_mensaje(texto):
    """Cuando dice el mensaje que se renueva el cupo, en segundos, o None."""
    m = _RENUEVA.search(str(texto or ""))
    return parse_duracion(m.group(1)) if m else None


_PISTAS_SESION = ("authentication required", "authentication failed",
                  "please visit the url to log in", "not logged in",
                  "unauthenticated", "invalid_grant", "log in again",
                  "sign in again", "session expired", "token has been expired",
                  "token expired", "credentials have expired", "please log in")
_PISTAS_CUPO = ("quota", "resource_exhausted", "resource exhausted",
                "exhausted your", "usage limit", "out of credits",
                "credits have been exhausted", "limit reached")
_PISTAS_CAPACIDAD = ("capacity", "overloaded", "high demand", "try again later",
                     "temporarily unavailable", "too many requests",
                     "rate limit", "rate_limit", "busy")
_PISTAS_SERVIDOR = ("internal error", "internal server", "server error",
                    "bad gateway", "gateway timeout", "service unavailable",
                    "unavailable")


def clasificar(texto):
    """(tipo, espera_s) a partir de lo que dijo agy.

    tipo: sesion | cupo | capacidad | servidor | tiempo | otro. Se mira el
    texto de ERROR (el campo `error` del JSON y la salida de error), nunca la
    respuesta del modelo: si la imagen hablara de «cuotas» no es un cupo.
    """
    bajo = " ".join(str(texto or "").lower().split())
    if any(p in bajo for p in _PISTAS_SESION) or re.search(r"\b401\b", bajo):
        return "sesion", None
    if any(p in bajo for p in _PISTAS_CUPO):
        return "cupo", espera_del_mensaje(bajo) or ESPERA_CUPO_S
    if any(p in bajo for p in _PISTAS_CAPACIDAD) or re.search(r"\b(429|503)\b", bajo):
        return "capacidad", espera_del_mensaje(bajo) or ESPERA_CAPACIDAD_S
    if any(p in bajo for p in _PISTAS_SERVIDOR) or re.search(r"\b(500|502|504)\b", bajo):
        return "servidor", ESPERA_SERVIDOR_S
    if "timed out" in bajo or "timeout" in bajo or "deadline exceeded" in bajo:
        return "tiempo", ESPERA_SERVIDOR_S
    return "otro", None


_URL = re.compile(r"https?://\S+")


def _limpio(texto, tope=300):
    """Sin enlaces (el de acceso lleva el reto PKCE de ese intento) y acortado."""
    texto = _URL.sub("<enlace>", str(texto or ""))
    texto = " ".join(texto.split())
    return texto[:tope]


def _error_de(tipo, texto, cuenta, espera_s=None):
    # una cuenta con nombre va entre «»; «la sesion por defecto de agy» ya es una frase
    quien = nombre_de(cuenta)
    citado = quien if cuenta.get("id") == DEFECTO else f"«{quien}»"
    detalle = _limpio(texto)
    if tipo == "sesion":
        if cuenta.get("id") == DEFECTO:
            mensaje = ("la sesión por defecto de agy ha caducado: entra en agy otra "
                       "vez, o añade una cuenta de Google en Configuración")
        else:
            mensaje = (f"la sesión de Google de {quien} ha caducado: entra otra vez "
                       f"en Configuración")
    elif tipo == "cupo":
        mensaje = f"la cuenta de Google {citado} se ha quedado sin cupo"
        if espera_s and espera_s != ESPERA_CUPO_S:
            mensaje += f" (se renueva en {_reloj_humano(espera_s)})"
        mensaje += f": {detalle}" if detalle else ""
    elif tipo == "capacidad":
        mensaje = f"Google está saturado para {citado}: {detalle}".rstrip(": ")
    elif tipo == "servidor":
        mensaje = f"Google ha devuelto un error del servidor con {citado}: {detalle}"
    elif tipo == "tiempo":
        mensaje = f"agy no ha contestado a tiempo con {citado}"
    else:
        mensaje = f"agy ha fallado con {citado}: {detalle}".rstrip(": ")
    err = _CLASES[tipo](mensaje)
    err.cuenta = cuenta.get("id", "")
    err.espera_s = espera_s
    return err


def _reloj_humano(segundos):
    segundos = int(max(0, segundos))
    if segundos >= 3600:
        return f"{segundos // 3600} h {(segundos % 3600) // 60} min"
    if segundos >= 60:
        return f"{segundos // 60} min"
    return f"{segundos} s"


# ------------------------------------------------------------------ ejecutar

def _ejecutar(cmd, *, env, cwd, timeout_s):
    """Lanza agy y espera. -> (returncode, stdout, stderr, motivo)

    `motivo` es '' | 'tiempo' | 'sesion'. Con la sesion caducada agy escribe
    «Authentication required» y se queda ESPERANDO un codigo 60 s: no hay nadie
    que se lo dé, asi que se corta en cuanto aparece y se dice que es la sesion.
    Es el punto que se sustituye en las pruebas: agy no se llama de verdad.
    """
    proceso = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **SIN_VENTANA)
    trozos = {"out": [], "err": []}
    pide_login = threading.Event()

    def leer(flujo, clave):
        try:
            for linea in iter(flujo.readline, b""):
                texto = linea.decode("utf-8", errors="replace")
                trozos[clave].append(texto)
                if "authentication required" in texto.lower():
                    pide_login.set()
        except (OSError, ValueError):
            pass

    hilos = [threading.Thread(target=leer, args=(proceso.stdout, "out"), daemon=True),
             threading.Thread(target=leer, args=(proceso.stderr, "err"), daemon=True)]
    for hilo in hilos:
        hilo.start()
    limite = time.monotonic() + timeout_s
    motivo = ""
    while proceso.poll() is None:
        if pide_login.is_set():
            motivo = "sesion"
            break
        if time.monotonic() > limite:
            motivo = "tiempo"
            break
        time.sleep(0.1)
    if motivo:
        try:
            proceso.kill()
        except OSError:
            pass
    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    for hilo in hilos:
        hilo.join(timeout=2)
    return (proceso.returncode if proceso.returncode is not None else -1,
            "".join(trozos["out"]), "".join(trozos["err"]), motivo)


def _leer_payload(salida):
    """El JSON de `--output-format json`, o {} si no hay ninguno."""
    salida = (salida or "").strip()
    if not salida:
        return {}
    try:
        datos = json.loads(salida)
        return datos if isinstance(datos, dict) else {}
    except ValueError:
        pass
    for linea in reversed(salida.splitlines()):
        linea = linea.strip()
        if linea.startswith("{"):
            try:
                datos = json.loads(linea)
                return datos if isinstance(datos, dict) else {}
            except ValueError:
                continue
    return {}


def _evaluar(resultado, cuenta):
    """De lo que devolvio agy al payload, o el error tipado. -> payload"""
    codigo, salida, error, motivo = resultado
    if motivo == "sesion":
        raise _error_de("sesion", "", cuenta)
    if motivo == "tiempo":
        raise _error_de("tiempo", "", cuenta)
    payload = _leer_payload(salida)
    fallo = payload.get("status") == "ERROR" or (codigo != 0 and not payload)
    if codigo != 0 or fallo:
        texto = " ".join(t for t in (str(payload.get("error") or ""),
                                     (error or "").strip()[-600:],
                                     "" if payload else (salida or "").strip()[-300:])
                         if t)
        tipo, espera = clasificar(texto)
        raise _error_de(tipo, texto or f"agy termino con codigo {codigo}", cuenta, espera)
    return payload


# ------------------------------------------------------------------ el reparto

def _paralelo():
    try:
        return max(1, int(os.environ.get("ESTUDIO_AGY_PARALELO") or 2))
    except ValueError:
        return 2


_SEMAFORO = {"n": 0, "obj": None}


def _semaforo():
    with _LOCK:
        n = _paralelo()
        if _SEMAFORO["obj"] is None or _SEMAFORO["n"] != n:
            _SEMAFORO["n"], _SEMAFORO["obj"] = n, threading.BoundedSemaphore(n)
        return _SEMAFORO["obj"]


def _espera_max():
    try:
        return float(os.environ.get("ESTUDIO_AGY_ESPERA_MAX") or 600)
    except ValueError:
        return 600.0


def _tomar_cuenta():
    """La cuenta con la que se llama ahora. Espera, con tope, si todas estan apartadas."""
    while True:
        cuentas = cuentas_usables()
        if not cuentas:
            raise SinCuentas(
                "no hay ninguna cuenta de Google con sesión: añade una en "
                "Configuración y entra con ella")
        salud = salud_leer()
        ahora = _reloj()
        esperas, sin_sesion = [], []
        for cuenta in cuentas:
            bloqueo = bloqueo_de(salud.get(cuenta["id"]), ahora)
            if bloqueo is None:
                return cuenta
            if bloqueo[0] == "sesion":
                sin_sesion.append(cuenta)
            else:
                esperas.append((bloqueo[1], cuenta))
        if not esperas:
            quienes = ", ".join(nombre_de(c) for c in sin_sesion)
            err = _error_de("sesion", "", sin_sesion[0])
            if len(sin_sesion) > 1:
                err = SesionCaducada(
                    f"la sesión de Google ha caducado en todas las cuentas "
                    f"({quienes}): entra otra vez en Configuración")
            raise err
        hasta, cuenta = min(esperas, key=lambda par: par[0])
        falta = hasta - ahora
        if falta > _espera_max():
            ficha = salud.get(cuenta["id"]) or {}
            clase = _CLASES.get({"cupo": "cupo", "capacidad": "capacidad",
                                 "tiempo": "tiempo"}.get(ficha.get("estado"), "servidor"))
            err = clase(
                f"todas las cuentas de Google están apartadas y la primera vuelve "
                f"en {_reloj_humano(falta)} ({nombre_de(cuenta)}: "
                f"{_limpio(ficha.get('mensaje'), 160)})")
            err.cuenta = cuenta["id"]
            err.espera_s = falta
            raise err
        _dormir(min(max(falta, 0.2), 5.0))


def _apuntar_fallo(cuenta, err, para=""):
    """Anota el fallo y aparta la cuenta el tiempo que toque."""
    ahora = _reloj()
    estado = {"sesion": "sesion", "cupo": "cupo", "capacidad": "capacidad",
              "tiempo": "tiempo"}.get(err.tipo, "error")
    espera = None
    if err.tipo in ("cupo", "capacidad", "servidor", "tiempo"):
        espera = err.espera_s or ESPERA_SERVIDOR_S
    anotar(cuenta["id"], estado, str(err), hasta=(ahora + espera) if espera else None,
           para=para)


# ------------------------------------------------------------------ imagenes

def _al_dia(destino, origen):
    """El destino existe y no es mas viejo que su origen."""
    try:
        return (os.path.exists(destino)
                and os.path.getmtime(destino) >= os.path.getmtime(origen))
    except OSError:
        return False


def normalizar(ruta, cache_dir, lado_max=1024):
    """PNG RGBA con el lado mayor acotado. Mismo nombre en la cache que el motor
    de OpenAI (huella de la ruta completa): las dos cosas escriben lo mismo.

    Se escribe a un temporal y se sustituye de golpe: las cadenas de planos van
    en paralelo y comparten referencias, y un PNG a medio escribir no se puede
    adjuntar.
    """
    os.makedirs(cache_dir, exist_ok=True)
    nombre, extension = os.path.splitext(os.path.basename(ruta))
    firma = hashlib.sha1(os.path.normcase(os.path.abspath(ruta)).encode("utf-8"))
    destino = os.path.join(cache_dir, f"{nombre}__{firma.hexdigest()[:8]}{extension}")
    if _al_dia(destino, ruta):
        return destino
    img = Image.open(ruta).convert("RGBA")
    if max(img.size) > lado_max:
        escala = lado_max / max(img.size)
        img = img.resize((int(img.width * escala), int(img.height * escala)),
                         Image.LANCZOS)
    temporal = "%s.%d.%d.tmp" % (destino, os.getpid(), threading.get_ident() & 0xffff)
    img.save(temporal, "PNG")
    for espera in (0.1, 0.25, 0.5, 1.0, 0):
        try:
            os.replace(temporal, destino)
            return destino
        except PermissionError:
            if espera:
                time.sleep(espera)
    # otro hilo dejo EXACTAMENTE este fichero (el nombre lleva la huella de la ruta)
    if _al_dia(destino, ruta):
        try:
            os.remove(temporal)
        except OSError:
            pass
        return destino
    os.replace(temporal, destino)
    return destino


def ajustar(im, destino):
    """Recorta al aspecto del lienzo (desde el centro) Y LUEGO escala al tamano.

    Escalar directamente estira: agy devuelve 1376x768 (16:9) y el lienzo es
    3:2, asi que un resize a secas aplastaria todo lo que sale en el plano.
    """
    ancho_d, alto_d = destino
    ancho, alto = im.size
    objetivo = ancho_d / alto_d
    if abs(ancho / alto - objetivo) > 0.002:
        if ancho / alto > objetivo:                    # mas ancha: se quitan los lados
            nuevo = max(1, round(alto * objetivo))
            izq = (ancho - nuevo) // 2
            caja = (izq, 0, izq + nuevo, alto)
        else:                                          # mas alta: arriba y abajo
            nuevo = max(1, round(ancho / objetivo))
            arriba = (alto - nuevo) // 2
            caja = (0, arriba, ancho, arriba + nuevo)
        im = im.crop(caja)
    if im.size != destino:
        im = im.resize(destino, Image.LANCZOS)
    return im


def _limitar_referencias(referencias):
    """(las que van, las que se dejan fuera). Recorta por el final, y se dice."""
    try:
        tope = max(1, int(os.environ.get("ESTUDIO_AGY_MAX_REFS") or MAX_REFERENCIAS))
    except ValueError:
        tope = MAX_REFERENCIAS
    return referencias[:tope], referencias[tope:]


def _ruta_agy_json(ruta):
    """Rutas con barras normales: dentro del JSON de la peticion una barra
    invertida hay que escaparla, y el modelo la copia a su manera."""
    return os.path.abspath(ruta).replace("\\", "/")


def _pedido(prompt, aspecto, refs, salida):
    lineas = ["Usa tu herramienta generate_image para generar la siguiente imagen.",
              f"Prompt: {prompt}",
              f"AspectRatio: '{aspecto}'"]
    if refs:
        lineas.append("ImagePaths: " + json.dumps([_ruta_agy_json(r) for r in refs]))
    lineas.append("Guarda la imagen generada exactamente en este archivo: "
                  + _ruta_agy_json(salida))
    return "\n".join(lineas)


def _una_llamada(cuenta, prompt, refs, tamano, timeout_s):
    """UNA llamada a agy con UNA cuenta. -> (png_bytes, payload, segundos)"""
    temp_dir = tempfile.mkdtemp(prefix="estudio_agy_img_")
    salida_png = os.path.join(temp_dir, "salida.png")
    try:
        cmd = [ruta_agy(),
               "-p", _pedido(prompt, ASPECT_RATIOS.get(tamano, "3:2"), refs, salida_png),
               "--output-format", "json",
               "--print-timeout", f"{int(timeout_s)}s",
               # hace falta para que agy ESCRIBA el fichero de salida sin
               # preguntar; solo va en la generacion, no en las pruebas
               "--dangerously-skip-permissions",
               "--add-dir", temp_dir]
        t0 = time.time()
        resultado = _ejecutar(cmd, env=entorno_de(cuenta), cwd=temp_dir,
                              timeout_s=timeout_s + 30)
        segundos = time.time() - t0
        payload = _evaluar(resultado, cuenta)

        fichero = None
        if os.path.exists(salida_png) and os.path.getsize(salida_png) > 0:
            fichero = salida_png
        else:
            for patron in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                encontrados = sorted(glob.glob(os.path.join(temp_dir, patron)))
                if encontrados:
                    fichero = encontrados[0]
                    break
        if not fichero:
            raise _error_de(
                "otro", "agy no generó el archivo de imagen esperado. Respuesta: "
                + _limpio(payload.get("response"), 200), cuenta)

        dimension = DIMENSIONES.get(tamano, (1536, 1024))
        try:
            with Image.open(fichero) as im:
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                im = ajustar(im, dimension)
                buffer = io.BytesIO()
                im.save(buffer, format="PNG")
        except (OSError, ValueError) as fallo:
            raise _error_de("otro", f"la imagen que dejó agy no se puede leer: {fallo}",
                            cuenta)
        return buffer.getvalue(), payload, segundos
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def generar(prompt, referencias=None, *, quality="low", tamano="apaisado",
            timeout_s=300, reintentos=None, **_ignorado):
    """Genera una imagen con agy. -> (png_bytes, meta)

    Reintenta con la SIGUIENTE cuenta cuando una falla (ver la cabecera). `meta`
    lleva `usage` (los tokens de agy), como el motor de OpenAI: es lo que lee el
    medidor de coste.
    """
    referencias = [str(r) for r in (referencias or [])]
    faltan = [r for r in referencias if not os.path.exists(r)]
    if faltan:
        raise ValueError("estas imagenes de referencia no existen: "
                         + ", ".join(faltan[:5]))
    if tamano not in TAMANOS:
        raise ValueError(f"tamano {tamano!r}: solo {', '.join(TAMANOS)}")
    enviadas, omitidas = _limitar_referencias(referencias)
    if omitidas:
        print(f"[imagen_agy] {len(referencias)} referencias: agy admite "
              f"{len(enviadas)}; se dejan fuera las {len(omitidas)} ultimas",
              flush=True)

    usables = cuentas_usables()
    extra = 2 if reintentos is None else int(reintentos)
    fallos = []
    for intento in range(1, max(1, len(usables)) + extra + 1):
        cuenta = _tomar_cuenta()
        try:
            with _semaforo():
                png, payload, segundos = _una_llamada(
                    cuenta, prompt, enviadas, tamano, timeout_s)
        except ErrorAgy as err:
            err.cuenta = cuenta.get("id", "")
            fallos.append(err)
            _apuntar_fallo(cuenta, err, para="generar una imagen")
            print(f"[imagen_agy] {nombre_de(cuenta)}: {err}", flush=True)
            continue
        anotar(cuenta["id"], "ok", "", para="generar una imagen")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return png, {
            "segundos": round(segundos, 1),
            "intentos": intento,
            "tamano": TAMANOS[tamano],
            "quality": quality,
            "refs": len(enviadas),
            "refs_omitidas": len(omitidas),
            "modelo": "agy",
            "motor": "agy",
            "cuenta": cuenta.get("id", ""),
            "coste": 0.0,
            "usage": usage,
        }
    ultimo = fallos[-1]
    if len(fallos) > 1:
        resto = "; ".join(f"{nombre_de({'id': f.cuenta})}: {f.tipo}" for f in fallos[:-1])
        ultimo.args = (f"{ultimo} (antes: {resto})",)
    raise ultimo


def probar(cuenta, para="probar la cuenta"):
    """UNA llamada minima a esa cuenta y se apunta lo que pase. -> ficha

    Es lo unico que puede decir «funciona»: preguntar por la sesion sin hablarle
    no distingue un cupo agotado de una cuenta sana. Solo se llama a peticion.
    """
    temp_dir = tempfile.mkdtemp(prefix="estudio_agy_ping_")
    try:
        cmd = [ruta_agy(), "-p", "Contesta solo con la palabra: ok",
               "--output-format", "json", "--print-timeout", "45s"]
        with _semaforo():
            resultado = _ejecutar(cmd, env=entorno_de(cuenta), cwd=temp_dir,
                                  timeout_s=60)
        _evaluar(resultado, cuenta)
    except ErrorAgy as err:
        err.cuenta = cuenta.get("id", "")
        _apuntar_fallo(cuenta, err, para=para)
        return salud_de(cuenta["id"])
    except OSError as fallo:
        return anotar(cuenta["id"], "error", f"no se pudo ejecutar agy: {fallo}",
                      para=para)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return anotar(cuenta["id"], "ok", "", para=para)


# ------------------------------------------------------------------ lotes

def generar_lote(trabajos, *, concurrencia=None):
    """Como el de OpenAI: un fallo no tumba el lote. La concurrencia real la
    limita `_semaforo` (ESTUDIO_AGY_PARALELO)."""
    from concurrent.futures import ThreadPoolExecutor

    def uno(trabajo):
        try:
            png, meta = generar(trabajo["prompt"], trabajo.get("referencias"),
                                quality=trabajo.get("quality", "low"),
                                tamano=trabajo.get("tamano", "apaisado"))
        except Exception as exc:                        # noqa: BLE001
            return {"id": trabajo.get("id"), "error": str(exc)}
        os.makedirs(os.path.dirname(trabajo["destino"]), exist_ok=True)
        with open(trabajo["destino"], "wb") as fh:
            fh.write(png)
        return {"id": trabajo.get("id"), "destino": trabajo["destino"], **meta}

    with ThreadPoolExecutor(max_workers=concurrencia or _paralelo()) as pool:
        return list(pool.map(uno, trabajos))


# nombres publicos para quien lanza agy por su cuenta (el login de la pantalla)
leer_payload = _leer_payload
limpio = _limpio
