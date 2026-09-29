"""
Pruebas del motor de imagen de Google (agy) y de todo lo que lo rodea.

NADA sale a agy de verdad ni gasta cupo: o se sustituye `_ejecutar` por un doble
(el reparto entre cuentas, los errores, el recorte) o se lanza un `agy` FALSO
--un script que imprime lo que imprimiria el de verdad-- por ESTUDIO_AGY (el
lanzamiento real, el HOME aislado, el corte al pedir login, el login por
pantalla).

Lo que se protege, por orden de riesgo:

  1. la FIRMA de cache: ni sin param, ni `openai`, ni `adoptar` la mueven (mover
     una firma deja obsoleto lo ya pagado); `agy` si es otra imagen
  2. que el estado que ve la pantalla NO lance agy (cada lanzamiento es una
     llamada al modelo y gasta cupo)
  3. el recorte al aspecto ANTES de escalar (escalar a secas estira)
  4. el reparto entre cuentas: orden, apartado por motivo, espera con tope,
     reintento con la siguiente, y la concurrencia
  5. los errores tipados con el mensaje que se ensena
  6. el coste: agy se anota (tokens, imagenes, segundos) sin dolares, y lo de
     OpenAI queda como estaba

    python pasos\\prueba_motor_agy.py
"""
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Lo que se toca en estas pruebas --secretos, ajustes-- va a una carpeta
# temporal: sin esto la salud de los dobles acabaria en el almacen de verdad.
CARPETA = tempfile.mkdtemp(prefix="prueba_agy_")
os.environ["ESTUDIO_SECRETOS"] = os.path.join(CARPETA, "secretos")
os.environ["ESTUDIO_AJUSTES"] = os.path.join(CARPETA, "ajustes.json")
os.environ.pop("ESTUDIO_AGY", None)

from PIL import Image, ImageDraw  # noqa: E402

import ajustes  # noqa: E402
import claves  # noqa: E402
import comprobar_claves  # noqa: E402
import login_agy  # noqa: E402
import medios  # noqa: E402
import p6_assets  # noqa: E402
from nucleo import coste  # noqa: E402

M = medios.motor("imagen_agy/imagen.py")           # el modulo que usa el Estudio

FALLOS = []


def ok(condicion, texto):
    print(("  ok   " if condicion else "  FALLO ") + texto)
    if not condicion:
        FALLOS.append(texto)


def igual(obtenido, esperado, texto):
    ok(obtenido == esperado,
       texto if obtenido == esperado
       else f"{texto}  [obtenido={obtenido!r} esperado={esperado!r}]")


def seccion(titulo):
    print(f"\n[{titulo}]")


def png(ruta, tam=(64, 48), color=(200, 30, 30)):
    Image.new("RGB", tam, color).save(ruta)
    return ruta


def png_bytes(tam, color=(10, 120, 200)):
    b = io.BytesIO()
    Image.new("RGB", tam, color).save(b, format="PNG")
    return b.getvalue()


# ------------------------------------------------------------------ 0. contratos

def prueba_contratos():
    seccion("0] contratos del motor")
    igual(M.TAMANOS["apaisado"], "1536x1024", "tamano apaisado")
    igual(M.DIMENSIONES["vertical"], (1024, 1536), "dimensiones vertical")
    igual(M.ASPECT_RATIOS["apaisado"], "3:2", "aspect ratio apaisado")
    ok("agy" in coste.PROVEEDORES and "agy" in coste.SIN_DOLARES,
       "agy es proveedor de coste y no suma dolares")
    igual(coste.ETIQUETAS["agy"], "Google (Antigravity)", "etiqueta de coste")
    igual(ajustes.POR_DEFECTO["motor_imagen"], "openai",
          "el motor por defecto sigue siendo openai")
    ok("agy" in ajustes.MOTORES_IMAGEN, "ajustes acepta agy")
    igual(ajustes.guardar({"motor_imagen": "agy"})["motor_imagen"], "agy",
          "y se guarda como ajuste")
    try:
        ajustes.guardar({"motor_imagen": "midjourney"})
        ok(False, "un motor desconocido se rechaza")
    except ValueError:
        ok(True, "un motor desconocido se rechaza")
    ajustes.guardar({"motor_imagen": "openai"})

    # UNA sola busqueda del ejecutable, y es la del motor
    os.environ["ESTUDIO_AGY"] = "/no/importa/agy"
    igual(M.ruta_agy(), "/no/importa/agy", "ESTUDIO_AGY manda sobre el PATH")
    igual(login_agy.ruta_agy(), "/no/importa/agy",
          "y el login pide la ruta al motor: no hay una segunda copia")
    os.environ.pop("ESTUDIO_AGY")
    src = open(os.path.join(RAIZ, "pasos", "login_agy.py"), encoding="utf-8").read()
    ok("def ruta_agy():\n    \"\"\"El ejecutable de agy: UNA sola busqueda" in src
       and "shutil.which" not in src.split("def ruta_agy")[1].split("def ")[0],
       "login_agy no busca el ejecutable por su cuenta")


# ------------------------------------------------------------------ 1. firma

def prueba_firma():
    seccion("1] la firma de cache no se mueve salvo con agy")
    carpeta = os.path.join(CARPETA, "refs")
    os.makedirs(carpeta)
    refs = [png(os.path.join(carpeta, f"r{i}.png"), color=(i * 40, 9, 9))
            for i in range(3)]
    prompt = "un barco en la niebla"
    for tamano in ("apaisado", "vertical"):
        antes = medios.huella({
            "prompt": prompt, "calidad": "low",
            "tamano": tamano if tamano != "apaisado" else None,
            "refs": [medios.huella_fichero(r) for r in refs]})
        for etiqueta, extra in (("sin motor_imagen", {}),
                                ("openai", {"motor_imagen": "openai"}),
                                ("adoptar", {"motor_imagen": "adoptar"}),
                                ("None", {"motor_imagen": None}),
                                ("vacio", {"motor_imagen": ""})):
            p = dict({"calidad": "low"}, **extra)
            igual(p6_assets._firma_de_imagen(prompt, refs, tamano, p), antes,
                  f"{tamano}: {etiqueta} = la firma de siempre")
        distinta = p6_assets._firma_de_imagen(
            prompt, refs, tamano, {"calidad": "low", "motor_imagen": "agy"})
        ok(distinta != antes, f"{tamano}: agy da otra firma (es otra imagen)")

    seccion("1b] quien decide el motor: un solo sitio")
    igual(medios.ruta_motor_de_imagen({"motor_imagen": "agy"}), "imagen_agy/imagen.py",
          "agy -> motor de Google")
    for valor in ("openai", "adoptar", None, "", "raro"):
        igual(medios.ruta_motor_de_imagen({"motor_imagen": valor}),
              "imagen_openai/imagen.py", f"{valor!r} -> OpenAI")
    igual(medios.ruta_motor_de_imagen(None), "imagen_openai/imagen.py",
          "sin params -> OpenAI")
    ok("agy" in medios.motor_de_imagen({"motor_imagen": "agy"}).__name__,
       "motor_de_imagen devuelve el modulo de agy")
    # Nadie mas lo escribe a mano: un proyecto de Google no puede llamar a OpenAI
    # ni por una referencia, una lamina o una correccion.
    for nombre in ("pasos/p6_assets.py", "pasos/moodboard.py"):
        texto = open(os.path.join(RAIZ, nombre), encoding="utf-8").read()
        ok('medios.motor("imagen_openai' not in texto,
           f"{nombre} no llama a OpenAI por su cuenta")
    reg = open(os.path.join(RAIZ, "motores/revision/regenerar.py"), encoding="utf-8").read()
    ok("import imagen as motor" not in reg and "cargar_motor_imagen" in reg,
       "regenerar.py elige el motor por argumento, sin importar `imagen` a secas")


# ------------------------------------------------------------------ 2. recorte

def prueba_recorte():
    seccion("2] recorte al aspecto y luego escala (nada se estira)")
    # 1376x768 (16:9) es lo que devuelve agy; el lienzo es 3:2
    im = Image.new("RGB", (1376, 768), (0, 0, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 111, 767], fill=(255, 0, 0))            # 112 px de borde a cada lado
    d.rectangle([1264, 0, 1375, 767], fill=(255, 0, 0))
    d.ellipse([688 - 300, 384 - 300, 688 + 300, 384 + 300], fill=(255, 255, 255))
    fuera = M.ajustar(im, (1536, 1024))
    igual(fuera.size, (1536, 1024), "1376x768 sale a 1536x1024 exactos")
    px = fuera.load()
    ok(px[0, 512][0] < 40 and px[1535, 512][0] < 40,
       "los bordes recortados no se cuelan (no queda rojo)")

    def extension(puntos):
        blancos = [i for i, c in puntos if c[0] > 200 and c[1] > 200 and c[2] > 200]
        return max(blancos) - min(blancos) + 1
    horizontal = extension([(x, px[x, 512]) for x in range(1536)])
    vertical = extension([(y, px[768, y]) for y in range(1024)])
    ok(abs(horizontal - vertical) <= 4,
       f"el circulo sigue siendo circulo ({horizontal} x {vertical}): no se ha estirado")
    igual(M.ajustar(Image.new("RGB", (1000, 1000)), (1024, 1536)).size, (1024, 1536),
          "cuadrado -> vertical: recorta lados y escala")
    igual(M.ajustar(Image.new("RGB", (1536, 1024)), (1536, 1024)).size, (1536, 1024),
          "ya del tamano: no se toca")
    igual(M.ajustar(Image.new("RGB", (3000, 1000)), (1024, 1024)).size, (1024, 1024),
          "muy apaisada -> cuadrado")


# ------------------------------------------------------------------ 3. errores

def prueba_errores():
    seccion("3] taxonomia de errores")
    for texto, tipo in (
            ("Authentication required. Please visit the URL to log in", "sesion"),
            ("authentication failed or timed out", "sesion"),
            ("HTTP 401 unauthenticated", "sesion"),
            ("RESOURCE_EXHAUSTED: You have exhausted your capacity on this model", "cupo"),
            ("quota exceeded for this account", "cupo"),
            ("429 Too Many Requests", "capacidad"),
            ("The model is overloaded. Please try again later.", "capacidad"),
            ("503 service unavailable", "capacidad"),
            ("500 Internal error encountered", "servidor"),
            ("deadline exceeded", "tiempo"),
            ("algo completamente distinto", "otro")):
        igual(M.clasificar(texto)[0], tipo, f"{texto[:44]!r} -> {tipo}")
    igual(M.clasificar("quota reset after 4h59m")[1], 17940.0,
          "el cupo lee cuando se renueva ('reset after 4h59m')")
    igual(M.clasificar("quota exhausted")[1], 5 * 3600, "sin fecha: 5 h")
    igual(M.clasificar("model overloaded")[1], 45, "capacidad: 45 s")
    igual(M.clasificar("500 internal error")[1], 20, "servidor: 20 s")
    igual(M.parse_duracion("5m30s"), 330.0, "5m30s = 330 s")
    igual(M.parse_duracion("17940s"), 17940.0, "17940s")
    igual(M.parse_duracion("nada"), None, "sin numero: None")

    cuenta = {"id": "a1", "etiqueta": "la mía", "home": ""}
    # de lo que devuelve agy al error tipado (nada de lanzar el proceso)
    for resultado, clase, trozo in (
            ((1, "", "Authentication required. Please visit https://accounts.google.com/o/x?code_challenge=SECRETO", "sesion"),
             M.SesionCaducada, "la sesión de Google de la mía ha caducado: entra otra vez en Configuración"),
            ((1, json.dumps({"status": "ERROR", "error": "RESOURCE_EXHAUSTED quota reset after 2h"}), "", ""),
             M.CupoAgotado, "sin cupo"),
            ((1, json.dumps({"status": "ERROR", "error": "model overloaded"}), "", ""),
             M.Saturado, "saturado"),
            ((1, json.dumps({"status": "ERROR", "error": "500 internal error"}), "", ""),
             M.ErrorServidor, "error del servidor"),
            ((-1, "", "", "tiempo"), M.TiempoAgotado, "no ha contestado a tiempo"),
            ((1, "", "algo raro", ""), M.ErrorAgy, "algo raro")):
        try:
            M._evaluar(resultado, cuenta)
            ok(False, f"{clase.__name__} se lanza")
        except M.ErrorAgy as err:
            ok(type(err) is clase and trozo in str(err),
               f"{clase.__name__}: {str(err)[:80]}")
            ok("SECRETO" not in str(err) and "https://" not in str(err),
               f"{clase.__name__}: el mensaje no lleva el enlace de acceso")
    payload = M._evaluar((0, json.dumps({"status": "SUCCESS", "usage": {"input_tokens": 3}}), "", ""), cuenta)
    igual(payload["usage"]["input_tokens"], 3, "una respuesta buena devuelve su payload")


# ------------------------------------------------------------------ 4. reparto

RELOJ = [1_000_000.0]
LLAMADAS = []
GUION = {}
_CANDADO = threading.Lock()
_VUELO = {"ahora": 0, "max": 0}
ULTIMO_CMD = {}


def _cuenta_del_entorno(env):
    home = env.get("HOME") if env else None
    for cuenta in M.cuentas_configuradas():
        if cuenta["home"] and home and os.path.abspath(cuenta["home"]) == os.path.abspath(home):
            return cuenta["id"]
    return M.DEFECTO


def falso(cmd, *, env, cwd, timeout_s):
    cuenta = _cuenta_del_entorno(env)
    with _CANDADO:
        pendientes = GUION.get(cuenta) or []
        modo = pendientes.pop(0) if pendientes else "ok"
        LLAMADAS.append(cuenta)
        ULTIMO_CMD["cmd"] = cmd
        ULTIMO_CMD["env"] = env
        _VUELO["ahora"] += 1
        _VUELO["max"] = max(_VUELO["max"], _VUELO["ahora"])
    try:
        if os.environ.get("PRUEBA_AGY_LENTO"):
            time.sleep(0.05)
        if modo == "ok":
            m = re.search(r"en este archivo: (.+)$", cmd[2], re.M)
            with open(m.group(1), "wb") as fh:
                fh.write(png_bytes((1376, 768)))
            return (0, json.dumps({"status": "SUCCESS", "response": "hecho",
                                   "usage": {"input_tokens": 100, "output_tokens": 20,
                                             "thinking_tokens": 5, "cache_read_tokens": 7,
                                             "total_tokens": 132}}), "", "")
        if modo == "sin_imagen":
            return (0, json.dumps({"status": "SUCCESS", "response": "no puedo"}), "", "")
        if modo == "cupo":
            return (1, json.dumps({"status": "ERROR", "error":
                                   "RESOURCE_EXHAUSTED: quota exhausted, reset after 4h59m"}), "", "")
        if modo == "cupo_sin_fecha":
            return (1, json.dumps({"status": "ERROR", "error": "quota exhausted"}), "", "")
        if modo == "sesion":
            return (1, "", "Authentication required. Please visit the URL", "sesion")
        if modo == "capacidad":
            return (1, json.dumps({"status": "ERROR", "error": "model overloaded (503)"}), "", "")
        if modo == "servidor":
            return (1, json.dumps({"status": "ERROR", "error": "500 Internal error"}), "", "")
        if modo == "tiempo":
            return (-1, "", "", "tiempo")
        raise AssertionError(f"modo desconocido {modo}")
    finally:
        with _CANDADO:
            _VUELO["ahora"] -= 1


def montar_cuentas(ids, entrada=True, activas=None):
    """claves.json con esas cuentas, cada una con su home. -> {id: home}"""
    homes = {}
    lista = []
    for i in ids:
        home = os.path.join(CARPETA, "secretos", "agy", i)
        os.makedirs(home, exist_ok=True)
        homes[i] = home
        lista.append({"id": i, "etiqueta": {"a1": "la mía", "a2": "la del curro",
                                            "a3": "reserva"}.get(i, i),
                      "home": home, "entrada": entrada,
                      "activa": True if activas is None else (i in activas)})
    os.makedirs(os.path.join(CARPETA, "secretos"), exist_ok=True)
    with open(M.ruta_claves(), "w", encoding="utf-8") as fh:
        json.dump({"agy": {"cuentas": lista}}, fh)
    if os.path.exists(M.ruta_salud()):
        os.remove(M.ruta_salud())
    LLAMADAS.clear()
    GUION.clear()
    return homes


def preparar_motor():
    M._ejecutar = falso
    M._reloj = lambda: RELOJ[0]
    M._dormir = lambda s: RELOJ.__setitem__(0, RELOJ[0] + s)


def prueba_reparto():
    seccion("4] reparto entre cuentas (orden, apartado, espera, reintento)")
    preparar_motor()
    refs = [png(os.path.join(CARPETA, "refs", f"r{i}.png")) for i in range(3)]

    montar_cuentas(["a1", "a2", "a3"])
    imagen, meta = M.generar("un barco", refs, tamano="apaisado")
    igual(LLAMADAS, ["a1"], "usa la PRIMERA cuenta sana en el orden del usuario")
    with Image.open(io.BytesIO(imagen)) as im:
        igual(im.size, (1536, 1024), "y el PNG sale a 1536x1024 exactos (entrada 1376x768)")
    igual(meta["motor"], "agy", "meta.motor = agy")
    igual(meta["usage"]["input_tokens"], 100, "meta lleva `usage` (como OpenAI), no `tokens`")
    ok("tokens" not in meta, "y no hay una clave `tokens` suelta")
    igual(meta["coste"], 0.0, "coste 0")
    igual(meta["cuenta"], "a1", "dice con que cuenta se hizo")
    igual(meta["tamano"], "1536x1024", "tamano como en OpenAI")
    igual(M.salud_de("a1")["estado"], "ok", "y apunta la salud: ok")
    igual(M.salud_de("a1")["hasta"], None, "sin apartado")

    # 4b. cupo en la primera -> la misma imagen sigue con la segunda
    montar_cuentas(["a1", "a2", "a3"])
    GUION["a1"] = ["cupo"]
    _, meta = M.generar("un barco", refs)
    igual(LLAMADAS, ["a1", "a2"], "cupo en la 1.ª: la imagen se reintenta con la 2.ª")
    igual(meta["cuenta"], "a2", "y se hizo con la 2.ª")
    ficha = M.salud_de("a1")
    igual(ficha["estado"], "cupo", "la 1.ª queda apuntada como sin cupo")
    igual(ficha["hasta"] - RELOJ[0], 17940, "apartada lo que dice el mensaje (4h59m)")
    LLAMADAS.clear()
    M.generar("otro", refs)
    igual(LLAMADAS, ["a2"], "la siguiente imagen ni prueba la apartada")

    # 4c. cupo sin fecha -> 5 h
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["cupo_sin_fecha"]
    M.generar("x", refs)
    igual(M.salud_de("a1")["hasta"] - RELOJ[0], 5 * 3600, "sin fecha en el mensaje: 5 h")

    # 4d. capacidad 45 s, y vuelve a entrar pasado el plazo
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["capacidad"]
    M.generar("x", refs)
    igual(M.salud_de("a1")["hasta"] - RELOJ[0], 45, "capacidad: 45 s")
    LLAMADAS.clear()
    M.generar("x", refs)
    igual(LLAMADAS, ["a2"], "dentro de los 45 s se usa la 2.ª")
    RELOJ[0] += 46
    LLAMADAS.clear()
    M.generar("x", refs)
    igual(LLAMADAS, ["a1"], "pasados los 45 s la 1.ª vuelve a mandar")

    # 4e. servidor 20 s
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["servidor"]
    M.generar("x", refs)
    igual(M.salud_de("a1")["hasta"] - RELOJ[0], 20, "error del servidor: 20 s")

    # 4f. sesion caducada: fuera hasta volver a entrar, por mucho que pase el tiempo
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["sesion"]
    M.generar("x", refs)
    igual(M.salud_de("a1")["estado"], "sesion", "sesion caducada apuntada")
    RELOJ[0] += 30 * 24 * 3600
    LLAMADAS.clear()
    M.generar("x", refs)
    igual(LLAMADAS, ["a2"], "un mes despues sigue fuera: no se reintenta sola")
    M.olvidar("a1")
    LLAMADAS.clear()
    M.generar("x", refs)
    igual(LLAMADAS, ["a1"], "al volver a entrar (olvidar) la 1.ª manda otra vez")

    # 4g. una sola cuenta con la sesion caducada: el mensaje que ve el usuario
    montar_cuentas(["a1"])
    GUION["a1"] = ["sesion"]
    try:
        M.generar("x", refs)
        ok(False, "sin cuenta viva la imagen falla")
    except M.SesionCaducada as err:
        igual(str(err), "la sesión de Google de la mía ha caducado: entra otra vez en Configuración",
              "mensaje: sesion caducada con el nombre de la cuenta")

    # 4h. todas apartadas, la primera vuelve pronto: se ESPERA y sigue
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["capacidad"]
    GUION["a2"] = ["capacidad"]
    t0 = RELOJ[0]
    _, meta = M.generar("x", refs)
    ok(RELOJ[0] - t0 >= 44, f"con todas apartadas espera a la que vuelve antes ({RELOJ[0] - t0:.0f} s)")
    igual(meta["cuenta"], "a1", "y sigue con la que volvio")

    # 4i. todas sin cupo (5 h): NO cuelga la tanda, falla claro
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["cupo_sin_fecha"]
    GUION["a2"] = ["cupo_sin_fecha"]
    t0 = RELOJ[0]
    try:
        M.generar("x", refs)
        ok(False, "todas sin cupo falla")
    except M.CupoAgotado as err:
        ok(RELOJ[0] - t0 < 60, "no espera horas: falla enseguida")
        ok("todas las cuentas de Google están apartadas" in str(err)
           and "5 h" in str(err), f"mensaje claro: {err}")

    # 4j. sin cuentas configuradas: la sesion por defecto de agy
    if os.path.exists(M.ruta_claves()):
        os.remove(M.ruta_claves())
    if os.path.exists(M.ruta_salud()):
        os.remove(M.ruta_salud())
    LLAMADAS.clear()
    M.generar("x", refs)
    igual(LLAMADAS, [M.DEFECTO], "sin cuentas: usa la sesion por defecto de agy")
    ok(M.DEFECTO not in [c["id"] for c in M.cuentas_configuradas()],
       "y la por defecto no cuenta como cuenta configurada")

    # 4k. cuentas configuradas pero ninguna con acceso: NO se cae a la por defecto
    montar_cuentas(["a1", "a2"], entrada=False)
    try:
        M.generar("x", refs)
        ok(False, "sin acceso hecho no se genera")
    except M.SinCuentas:
        ok(True, "cuentas sin acceso: error claro, sin usar la sesion por defecto")
    igual(LLAMADAS, [], "y no se llamo a nada")
    montar_cuentas(["a1", "a2"], activas=["a2"])
    M.generar("x", refs)
    igual(LLAMADAS, ["a2"], "una cuenta desactivada no se usa")

    # 4l. un fallo sin cupo ni sesion (no genero fichero) se reintenta y acaba
    montar_cuentas(["a1", "a2"])
    GUION["a1"] = ["sin_imagen", "sin_imagen", "sin_imagen", "sin_imagen"]
    try:
        M.generar("x", refs, reintentos=1)
        ok(False, "sin imagen repetido acaba fallando")
    except M.ErrorAgy as err:
        ok("no generó el archivo" in str(err), "y dice que agy no genero la imagen")
        ok(len(LLAMADAS) <= 3, f"con tope de intentos ({len(LLAMADAS)})")


def prueba_referencias():
    seccion("5] todas las referencias van, en su orden")
    preparar_motor()
    montar_cuentas(["a1"])
    carpeta = os.path.join(CARPETA, "refs5")
    os.makedirs(carpeta)
    refs = [png(os.path.join(carpeta, f"r{i:02d}.png")) for i in range(16)]

    def enviadas():
        m = re.search(r"ImagePaths: (\[.*\])", ULTIMO_CMD["cmd"][2])
        return json.loads(m.group(1)) if m else []

    _, meta = M.generar("x", refs[:6])
    esperadas = [os.path.abspath(r).replace("\\", "/") for r in refs[:6]]
    igual(enviadas(), esperadas, "6 referencias: van las 6, en el orden dado (no solo 3)")
    igual(meta["refs"], 6, "meta.refs = 6")
    igual(meta["refs_omitidas"], 0, "ninguna omitida")
    ok("\\" not in ULTIMO_CMD["cmd"][2].split("ImagePaths:")[1].split("\n")[0],
       "las rutas van con barras normales (nada que escapar)")
    _, meta = M.generar("x", refs)
    igual(len(enviadas()), 14, "16 referencias: se pasa el tope de 14")
    igual(enviadas(), [os.path.abspath(r).replace("\\", "/") for r in refs[:14]],
          "recortando por el final")
    igual((meta["refs"], meta["refs_omitidas"]), (14, 2),
          "y se deja dicho en meta (refs=14, refs_omitidas=2)")
    _, meta = M.generar("sin refs", [])
    ok("ImagePaths" not in ULTIMO_CMD["cmd"][2], "sin referencias no se manda ImagePaths")
    ok("--dangerously-skip-permissions" in ULTIMO_CMD["cmd"], "la generacion lo lleva")
    try:
        M.generar("x", ["/no/existe.png"])
        ok(False, "una referencia inexistente se rechaza")
    except ValueError:
        ok(True, "una referencia inexistente se rechaza")


def prueba_concurrencia():
    seccion("6] concurrencia modesta")
    preparar_motor()
    montar_cuentas(["a1", "a2"])
    os.environ["ESTUDIO_AGY_PARALELO"] = "2"
    os.environ["PRUEBA_AGY_LENTO"] = "1"
    _VUELO["max"] = 0
    hilos, errores = [], []

    def uno():
        try:
            M.generar("x", [])
        except Exception as fallo:            # noqa: BLE001
            errores.append(fallo)
    try:
        for _ in range(6):
            hilos.append(threading.Thread(target=uno))
            hilos[-1].start()
        for h in hilos:
            h.join(30)
    finally:
        os.environ.pop("PRUEBA_AGY_LENTO", None)
    igual(errores, [], "6 imagenes a la vez, ninguna falla")
    igual(_VUELO["max"], 2, "nunca mas de 2 llamadas a agy en vuelo (ESTUDIO_AGY_PARALELO=2)")
    os.environ.pop("ESTUDIO_AGY_PARALELO")


# ------------------------------------------------------------------ 7. agy falso

FALSO_PY = r'''
import json, os, sys, time
marca = os.environ.get("FAKE_AGY_MARCA")
if marca:
    with open(marca, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"home": os.environ.get("HOME"),
                             "userprofile": os.environ.get("USERPROFILE"),
                             "ssh": os.environ.get("SSH_CONNECTION"),
                             "args": sys.argv[1:]}) + "\n")
modo = os.environ.get("FAKE_AGY_MODO", "ok")
if modo == "ok":
    print(json.dumps({"status": "SUCCESS", "response": "ok", "usage": {"input_tokens": 1}}))
elif modo == "cupo":
    print(json.dumps({"status": "ERROR", "error": "RESOURCE_EXHAUSTED quota reset after 4h59m"}))
    sys.exit(1)
elif modo == "auth":
    sys.stderr.write("Authentication required. Please visit the URL to log in:\n"
                     "  https://accounts.google.com/o/oauth2/auth?code_challenge=SECRETO\n")
    sys.stderr.flush()
    time.sleep(40)
elif modo == "login":
    print("Authentication required. Please visit the URL to log in:")
    print("  https://accounts.google.com/o/oauth2/auth?client_id=x&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback")
    print("")
    print("Waiting for authentication (timeout 60s)...")
    print("Or, paste the authorization code here and press Enter:", flush=True)
    codigo = sys.stdin.readline().strip()
    if codigo == "bueno":
        print(json.dumps({"status": "SUCCESS", "response": "ok", "usage": {"input_tokens": 1}}))
    else:
        print(json.dumps({"status": "ERROR", "error": "invalid authorization code"}))
        sys.exit(1)
elif modo == "login_cuelga":
    print("Authentication required. Please visit the URL to log in:")
    print("  https://accounts.google.com/o/oauth2/auth?client_id=x", flush=True)
    time.sleep(60)
'''


def crear_agy_falso():
    carpeta = os.path.join(CARPETA, "falso")
    os.makedirs(carpeta, exist_ok=True)
    script = os.path.join(carpeta, "agy_falso.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(FALSO_PY)
    if os.name == "nt":
        ejecutable = os.path.join(carpeta, "agy.cmd")
        with open(ejecutable, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
    else:
        ejecutable = os.path.join(carpeta, "agy")
        with open(ejecutable, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(ejecutable, os.stat(ejecutable).st_mode | stat.S_IEXEC)
    return ejecutable


def prueba_agy_falso():
    seccion("7] agy FALSO: lanzamiento real, HOME aislado, corte al pedir login")
    ejecutable = crear_agy_falso()
    marca = os.path.join(CARPETA, "marca.jsonl")
    os.environ["ESTUDIO_AGY"] = ejecutable
    os.environ["FAKE_AGY_MARCA"] = marca
    # el motor SIN sustitutos: se lanza el proceso de verdad
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "agy_real", os.path.join(RAIZ, "motores", "imagen_agy", "imagen.py"))
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    montar_cuentas(["a1", "a2"])
    homes = {c["id"]: c["home"] for c in real.cuentas_configuradas()}

    # ---- LO QUE VE LA PANTALLA NO LANZA agy
    if os.path.exists(marca):
        os.remove(marca)
    login_agy._motor = lambda: real          # el motor sin sustitutos
    for _ in range(3):
        estado = login_agy.estado_para_pantalla()
        login_agy.mirar("a1")
    ok(not os.path.exists(marca), "estado_para_pantalla / mirar NO lanzan agy (0 lanzamientos)")
    igual([c["id"] for c in estado["cuentas"]], ["a1", "a2"], "y listan las cuentas en orden")
    igual(estado["cuentas"][0]["salud"], None, "sin llamadas reales todavia: 'sin probar'")
    ok(estado["instalado"] is True, "instalado sale de mirar el disco")
    ok(not os.path.exists(marca), "instalado tampoco lanza agy")

    # ---- probar SI habla, con el HOME de esa cuenta
    os.environ["FAKE_AGY_MODO"] = "ok"
    ficha = real.probar({"id": "a1", "etiqueta": "la mía", "home": homes["a1"]})
    igual(ficha["estado"], "ok", "Probar: la cuenta contesta")
    lineas = [json.loads(x) for x in open(marca, encoding="utf-8").read().splitlines()]
    igual(len(lineas), 1, "exactamente UN lanzamiento")
    igual(os.path.abspath(lineas[0]["home"]), os.path.abspath(homes["a1"]),
          "con HOME = la carpeta aislada de esa cuenta")
    igual(os.path.abspath(lineas[0]["userprofile"]), os.path.abspath(homes["a1"]),
          "y USERPROFILE tambien")
    ok(lineas[0]["ssh"], "con SSH_CONNECTION puesto (fuerza el flujo enlace+codigo)")
    ok("--dangerously-skip-permissions" not in lineas[0]["args"],
       "la prueba NO lleva --dangerously-skip-permissions")
    igual(real.salud_de("a1")["estado"], "ok", "y queda apuntada")
    estado = login_agy.estado_para_pantalla()
    igual(estado["cuentas"][0]["salud"]["estado"], "ok",
          "la pantalla ve la salud apuntada, sin lanzar nada mas")
    igual(len(open(marca, encoding="utf-8").read().splitlines()), 1,
          "leer el estado despues de probar no lanza otra vez")

    # LA SESION POR DEFECTO va SIN SSH_CONNECTION (con ella agy no usa el llavero
    # del sistema, que es donde vive esa sesion) y no hereda la del servicio
    os.environ["SSH_CONNECTION"] = "1.2.3.4 1 5.6.7.8 22"
    try:
        real.probar(real.cuenta_por_defecto())
    finally:
        os.environ.pop("SSH_CONNECTION", None)
    ultima = json.loads(open(marca, encoding="utf-8").read().splitlines()[-1])
    igual(ultima["ssh"], None, "la sesion por defecto se lanza SIN SSH_CONNECTION (ni heredada)")
    real.olvidar(real.DEFECTO)

    # ---- cupo real (JSON de error, codigo 1)
    os.environ["FAKE_AGY_MODO"] = "cupo"
    ficha = real.probar({"id": "a2", "etiqueta": "la del curro", "home": homes["a2"]})
    igual(ficha["estado"], "cupo", "cupo agotado clasificado en un lanzamiento real")
    ok(ficha["hasta"] and ficha["hasta"] - time.time() > 17000, "con su hora de vuelta")
    igual(login_agy.estado_para_pantalla()["cuentas"][1]["apartada_s"] > 17000, True,
          "la pantalla ve cuanto le queda apartada")

    # ---- sesion caducada: agy se queda esperando 40 s y se corta al momento
    os.environ["FAKE_AGY_MODO"] = "auth"
    t0 = time.time()
    ficha = real.probar({"id": "a1", "etiqueta": "la mía", "home": homes["a1"]})
    ok(time.time() - t0 < 15, f"agy pidiendo login se corta enseguida ({time.time() - t0:.1f} s, no 40)")
    igual(ficha["estado"], "sesion", "sesion caducada")
    ok("ha caducado" in ficha["mensaje"] and "SECRETO" not in ficha["mensaje"]
       and "https" not in ficha["mensaje"], "y el mensaje no lleva el enlace de acceso")

    # ---- probar_todas: una ficha por cuenta, y solo con cuentas
    os.environ["ESTUDIO_SIMULAR"] = "1"
    try:
        montar_cuentas(["a1", "a2"])
        fichas = comprobar_claves.probar_todas(cuentas_claude=[{"config_dir": "", "etiqueta": "x"}])
        igual([f["proveedor"] for f in fichas],
              ["openai", "cartesia", "jamendo", "freesound", "claude", "agy", "agy"],
              "probar_todas: con 2 cuentas de Google salen 2 fichas de agy")
        os.remove(M.ruta_claves())
        fichas = comprobar_claves.probar_todas(cuentas_claude=[{"config_dir": "", "etiqueta": "x"}])
        igual([f["proveedor"] for f in fichas],
              ["openai", "cartesia", "jamendo", "freesound", "claude"],
              "sin cuentas de Google, agy no sale (y no finge un 'ok')")
    finally:
        os.environ.pop("ESTUDIO_SIMULAR", None)

    # ---- login por pantalla, con el flujo enlace + codigo
    seccion("7b] login por pantalla (agy falso)")
    montar_cuentas(["a1"], entrada=False)
    os.environ["FAKE_AGY_MODO"] = "login"
    ficha = login_agy.entrar("a1", homes["a1"])
    limite = time.time() + 15
    while time.time() < limite and (login_agy.mirar("a1") or {}).get("estado") == "abriendo":
        time.sleep(0.1)
    v = login_agy.mirar("a1")
    igual(v["estado"], "enlace", "sale el enlace")
    ok(v["enlace"].startswith("https://accounts.google.com/"), "y es el de Google")
    ok(v["ventana_s"] == 60 and 0 < v["restan_s"] <= 60, f"con cuenta atras de 60 s ({v['restan_s']})")
    igual(login_agy.entrar("a1", homes["a1"])["enlace"], v["enlace"],
          "volver a entrar reencuentra el intento, no arranca otro")
    ficha = login_agy.pegar("a1", "bueno")
    igual(ficha["estado"], "dentro", "el codigo bueno deja dentro")
    ok(not claves.cuentas_agy(), "(todavia no cuenta para el reparto: falta darla por dentro)")
    login_agy.reconciliar()
    ok(bool(claves.cuentas_agy()), "reconciliar da por dentro la cuenta cuyo login acabo despues de responder")
    igual(real.salud_de("a1")["estado"], "ok", "y la apunta sana")

    claves.apuntar_cuenta_agy("a1", entrada=False)
    os.environ["FAKE_AGY_MODO"] = "login"
    login_agy._INTENTOS.clear()
    login_agy.entrar("a1", homes["a1"])
    limite = time.time() + 15
    while time.time() < limite and (login_agy.mirar("a1") or {}).get("estado") != "enlace":
        time.sleep(0.1)
    ficha = login_agy.pegar("a1", "malo")
    igual(ficha["estado"], "fallo", "un codigo malo falla")
    ok("no ha aceptado el código" in ficha["mensaje"], f"con su motivo: {ficha['mensaje']}")

    os.environ["FAKE_AGY_MODO"] = "login_cuelga"
    login_agy._INTENTOS.clear()
    login_agy.entrar("a1", homes["a1"])
    limite = time.time() + 15
    while time.time() < limite and (login_agy.mirar("a1") or {}).get("estado") != "enlace":
        time.sleep(0.1)
    ok(login_agy.reiniciar("a1", homes["a1"])["estado"] in ("abriendo", "enlace"),
       "reiniciar pide un enlace nuevo cuando se acaba la cuenta atras")
    ok(login_agy.cancelar("a1"), "cancelar tira el intento")
    igual(login_agy.mirar("a1"), None, "y no queda nada en memoria")
    login_agy.olvidar_todos()

    # ---- carpetas: solo se borra lo que cuelga de la base
    base = login_agy.carpeta_base()
    propia = login_agy.asegurar_carpeta("borrame")
    open(os.path.join(propia, "x"), "w").close()
    ok(login_agy.borrar_carpeta(propia) and not os.path.exists(propia), "quitar borra el HOME de la cuenta")
    for mala in (CARPETA, base, os.path.dirname(base), "", os.path.join(base, "..")):
        try:
            login_agy.borrar_carpeta(mala)
            ok(False, f"no se borra {mala!r}")
        except login_agy.ErrorLogin:
            ok(True, f"no se borra fuera de la base: {mala!r}")
    try:
        login_agy.carpeta_de("../malo")
        ok(False, "un id con .. se rechaza")
    except login_agy.ErrorLogin:
        ok(True, "un id con .. se rechaza")

    os.environ.pop("ESTUDIO_AGY", None)
    os.environ.pop("FAKE_AGY_MARCA", None)
    os.environ.pop("FAKE_AGY_MODO", None)


# ------------------------------------------------------------------ 8. claves

def prueba_claves():
    seccion("8] cuentas de Google en el almacen")
    if os.path.exists(claves.FICHERO):
        os.remove(claves.FICHERO)
    igual(claves.leer()["agy"]["cuentas"], [], "sin nada, sin cuentas")
    login_agy._motor = lambda: M
    r = claves.guardar({"agy": [{"etiqueta": "la mía"}, {"etiqueta": "curro"}]})
    igual([c["id"] for c in r["agy"]["cuentas"]], ["agy1", "agy2"], "ids nuevos")
    ok(all(c["activa"] for c in r["agy"]["cuentas"]), "nacen activas")
    ok("home" not in r["agy"]["cuentas"][0], "el resumen no lleva la carpeta")
    claves.apuntar_cuenta_agy("agy1", home=os.path.join(CARPETA, "h1"), entrada=True)
    r = claves.guardar({"agy": [{"id": "agy2", "etiqueta": "curro", "activa": False},
                                {"id": "agy1", "etiqueta": "mía"}]})
    igual([c["id"] for c in r["agy"]["cuentas"]], ["agy2", "agy1"], "el orden es el que manda la pantalla")
    a1 = claves.cuentas_agy(solo_listas=False)[1]
    ok(a1["entrada"] and a1["home"].endswith("h1"), "la pantalla NO puede tocar home ni entrada")
    igual(a1["etiqueta"], "mía", "pero si la etiqueta")
    igual([c["id"] for c in claves.cuentas_agy()], ["agy1"],
          "solo_listas deja fuera la desactivada y la sin acceso")
    try:
        claves.guardar({"agy": [{"id": "agy1"}, {"id": "agy1"}]})
        ok(False, "ids repetidos")
    except claves.ErrorClaves:
        ok(True, "ids repetidos se rechazan")
    try:
        claves.guardar({"agy": [{"etiqueta": str(i)} for i in range(claves.MAX_AGY + 1)]})
        ok(False, "tope de cuentas")
    except claves.ErrorClaves:
        ok(True, f"mas de {claves.MAX_AGY} cuentas se rechaza")
    igual(len(claves.cuentas_agy(solo_listas=False)), 2, "y el rechazo no dejo nada a medias")
    # lo que guarda claves.json lo lee el motor por contrato
    igual([c["id"] for c in M.cuentas_configuradas()], ["agy2", "agy1"],
          "el motor lee la misma lista por contrato (sin importar codigo del Estudio)")
    if os.path.exists(claves.FICHERO):
        os.remove(claves.FICHERO)
    ok("import claves" not in open(os.path.join(RAIZ, "motores", "imagen_agy", "imagen.py"),
                                   encoding="utf-8").read(),
       "el motor no importa codigo del Estudio")


# ------------------------------------------------------------------ 9. coste

def prueba_coste():
    seccion("9] coste: agy se anota sin dolares, OpenAI queda igual")
    anotados = []
    original = coste._anotar
    coste._anotar = lambda proveedor, operacion, unidad=None, **campos: anotados.append(
        (proveedor, operacion, campos))
    try:
        def generar_agy(prompt, referencias, **kw):
            return b"png", {"motor": "agy", "modelo": "agy", "tamano": "1536x1024",
                            "segundos": 12.5, "refs": 3, "cuenta": "a1", "coste": 0.0,
                            "usage": {"input_tokens": 100, "output_tokens": 20,
                                      "thinking_tokens": 5, "cache_read_tokens": 7}}
        _, meta = coste._medir_imagen(generar_agy)("p", [], quality="low")
        igual(len(anotados), 1, "una imagen de agy se anota")
        proveedor, operacion, campos = anotados[0]
        igual((proveedor, operacion), ("agy", "imagen"), "proveedor agy, operacion imagen")
        igual(campos["tokens"], {"entrada": 100, "salida": 25, "cache": 7},
              "tokens de entrada, salida (+pensados) y cache")
        igual(campos["cantidad"], {"imagenes": 1}, "una imagen")
        igual(campos["usd"], None, "sin dolares")
        igual(campos["detalle"]["segundos"], 12.5, "con los segundos (para las previsiones)")
        igual(meta["coste"], 0.0, "y meta.coste = 0")

        anotados.clear()

        def generar_openai(prompt, referencias, **kw):
            return b"png", {"modelo": "gpt-image-2", "tamano": "1536x1024", "quality": "low",
                            "segundos": 30.0, "refs": 9, "coste": 0.006,
                            "usage": {"input_tokens": 5114, "output_tokens": 196}}
        coste._medir_imagen(generar_openai)("p", [], quality="low")
        igual(anotados[0][0], "openai", "OpenAI sigue por su camino de siempre")
    finally:
        coste._anotar = original

    # de verdad, con un proyecto: el evento entra y se agrega
    registros = [{"proveedor": "agy", "tokens": {"entrada": 100, "salida": 25, "cache": 7},
                  "cantidad": {"imagenes": 2}, "usd": None}]
    r = coste.agregar(registros)
    igual(r["proveedores"]["agy"]["etiqueta"], "Google (Antigravity)", "etiqueta en el desglose")
    igual(r["proveedores"]["agy"]["cantidad"]["imagenes"], 2, "cuenta imagenes")
    igual(r["total_usd"], 0.0, "no suma al total")
    ok("Google  2 img" in r["cabecera"], f"la cabecera lo dice: {r['cabecera']}")
    sin = coste.agregar([])
    ok("Google" not in sin["cabecera"], "sin imagenes de Google, la cabecera es la de siempre")
    igual(sin["cabecera"],
          "OpenAI  $0.00 · 0 tok     TTS  $0.00 · 0 car     Claude  0 tok     TOTAL  $0.00",
          "byte a byte")

    modulos = coste.modulos_de_imagen()
    ok(any("imagen_agy" in (getattr(m, "__file__", "") or "") for m in modulos),
       "el medidor engancha tambien el motor de agy")


def main():
    try:
        prueba_contratos()
        prueba_firma()
        prueba_recorte()
        prueba_errores()
        prueba_reparto()
        prueba_referencias()
        prueba_concurrencia()
        prueba_claves()
        prueba_coste()
        prueba_agy_falso()
    finally:
        shutil.rmtree(CARPETA, ignore_errors=True)
    print(f"\nResultado: {len(FALLOS)} fallos")
    for f in FALLOS:
        print("  -", f)
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
