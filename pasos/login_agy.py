"""Entrar con una cuenta de Google (agy) desde la pantalla.

Gemelo de `login_cli.py` para Antigravity CLI. Cada cuenta vive en su PROPIO
HOME aislado (`<secretos>/agy/<id>/`), y el login se hace entero desde el
navegador: sale un enlace, se entra con Google, se pega el codigo.

COMO SE COMPORTA agy, MEDIDO
----------------------------
Sin sesion, `agy -p ...` con SSH_CONNECTION puesto (lo pone `entorno_de`: fuerza
el flujo de enlace en vez de abrir un navegador del servidor, y hace que el login
viva en ficheros bajo el HOME de la cuenta en vez de en el llavero del sistema,
que es uno solo para todos) escribe:

    Authentication required. Please visit the URL to log in:
      https://accounts.google.com/o/oauth2/auth?...
    Waiting for authentication (timeout 60s)...
    Or, paste the authorization code here and press Enter:

y se queda leyendo stdin. **Tiene 60 s** desde ahi: pasado ese plazo termina con
`authentication failed or timed out`. Por eso la pantalla enseña una cuenta
atras y deja pedir otro enlace.

LO QUE NO HACE, y es a proposito
--------------------------------
NO lanza agy para «mirar si hay sesion»: cada lanzamiento es una llamada al
modelo y gasta cupo. El estado que se ensena sale de lo que apunto el motor la
ultima vez que hablo de verdad con la cuenta (`imagen_agy.salud_de`), y solo el
boton «Probar» habla. Lo unico que lee `mirar()` es el intento en memoria.

`ruta_agy` y el resto de lo comun viven en el motor (`imagen_agy/imagen.py`), que
es quien lanza agy; aqui se piden por `medios.motor`. Un motor no importa codigo
del Estudio, pero el Estudio si puede importar un motor.
"""
import os
import re
import shutil
import subprocess
import threading
import time

try:
    from . import claves
except ImportError:  # ejecutado con la carpeta pasos en sys.path
    import claves

#: La ventana que da agy para pegar el codigo desde que enseña el enlace.
ESPERA_ENLACE_S = 60
#: Lo que se deja a agy para acabar despues de mandarle el codigo.
ESPERA_CODIGO_S = 120
#: Un intento abandonado se limpia solo pasado esto.
CADUCIDAD_INTENTO_S = 20 * 60

_ENLACE = re.compile(r"https://\S*(?:accounts\.google\.com|antigravity\.google)\S*", re.I)
_ID_VALIDO = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

_LOCK = threading.RLock()
#: {cuenta_id: Intento}. En memoria: un login a medias no sobrevive al servicio.
_INTENTOS = {}

SIN_VENTANA = {}
if os.name == "nt":
    SIN_VENTANA["creationflags"] = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


class ErrorLogin(RuntimeError):
    """Error mostrable al usuario."""


def _motor():
    # tarde y a proposito: `medios` arrastra el nucleo y este modulo se importa
    # pronto (pasos/__init__), antes que los motores pesados
    try:
        from . import medios                          # noqa: PLC0415
    except ImportError:
        import medios                                 # noqa: PLC0415
    return medios.motor("imagen_agy/imagen.py")


def ruta_agy():
    """El ejecutable de agy: UNA sola busqueda, la del motor."""
    return _motor().ruta_agy()


def instalado():
    return _motor().instalado()


def carpeta_base():
    """Donde vive el HOME de cada cuenta. Se mira el entorno EN CADA LLAMADA."""
    return os.path.join(_motor().carpeta_secretos(), "agy")


def carpeta_de(cuenta_id):
    if not _ID_VALIDO.match(str(cuenta_id or "")):
        raise ErrorLogin(f"id de cuenta no valido: {cuenta_id!r}")
    return os.path.join(carpeta_base(), cuenta_id)


def asegurar_carpeta(cuenta_id):
    carpeta = carpeta_de(cuenta_id)
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def _dentro_de_la_base(carpeta):
    """Solo se borra lo que cuelga de la carpeta de cuentas: una ruta puesta a
    mano en el almacen no puede convertir «quitar la cuenta» en borrar otra cosa."""
    if not carpeta:
        return False
    base = os.path.normcase(os.path.abspath(carpeta_base()))
    destino = os.path.normcase(os.path.abspath(carpeta))
    return destino != base and os.path.commonpath([base, destino]) == base


def borrar_carpeta(carpeta):
    """Borra el HOME de una cuenta (su login incluido). -> bool"""
    if not _dentro_de_la_base(carpeta):
        raise ErrorLogin("esa carpeta no es de una cuenta gestionada por el Estudio")
    if not os.path.isdir(carpeta):
        return False
    shutil.rmtree(carpeta, ignore_errors=True)
    return not os.path.exists(carpeta)


def salir(carpeta):
    """Cierra la sesion de una cuenta: se borra su HOME. -> (ok, dicho)

    Con el HOME aislado la sesion vive ahi dentro, asi que borrarlo ES cerrarla.
    La carpeta por defecto de agy no se toca nunca.
    """
    if not carpeta:
        return False, "esta cuenta usa la sesión por defecto de agy: se cierra desde agy"
    try:
        borrar_carpeta(carpeta)
    except ErrorLogin as fallo:
        return False, str(fallo)
    return True, "sesión cerrada"


# ------------------------------------------------------------------ el intento

class Intento:
    """Un login en marcha."""

    def __init__(self, cuenta_id, carpeta):
        self.cuenta_id = cuenta_id
        self.carpeta = carpeta
        self.estado = "abriendo"
        self.enlace = ""
        self.mensaje = ""
        self.arranque = time.time()
        self.enlace_en = None
        self.codigo_en = None
        self.proceso = None
        self._salida = []
        self._lock = threading.RLock()

    # -- lo que ve la pantalla
    def ver(self):
        with self._lock:
            restan = None
            if self.estado == "enlace" and self.enlace_en:
                restan = max(0, int(ESPERA_ENLACE_S - (time.time() - self.enlace_en)))
            return {
                "estado": self.estado,
                "enlace": self.enlace,
                "mensaje": self.mensaje,
                "segundos": int(time.time() - self.arranque),
                "ventana_s": ESPERA_ENLACE_S,
                # lo que queda de los 60 s de agy para pegar el codigo
                "restan_s": restan,
                "caduca_en": restan if restan is not None else 0,
            }

    def caducado(self):
        return (time.time() - self.arranque) > CADUCIDAD_INTENTO_S

    def vivo(self):
        return self.estado in ("abriendo", "enlace", "probando")

    def cancelar(self):
        with self._lock:
            self._matar()
            if self.vivo():
                self.estado = "fallo"
                self.mensaje = "cancelado"

    def _matar(self):
        if self.proceso is not None and self.proceso.poll() is None:
            try:
                self.proceso.kill()
            except OSError:
                pass

    # -- arranque
    def arrancar(self):
        motor = _motor()
        cmd = [motor.ruta_agy(), "-p", "ping", "--output-format", "json"]
        # EN LINUX LA ENTRADA VA POR UN PSEUDO-TERMINAL, no por una tuberia.
        # MEDIDO en la imagen de Docker: con stdin en tuberia agy cree que le
        # pasan el prompt por ahi y contesta «authentication required. Run
        # 'agy' to log in» sin ofrecer enlace; con /dev/null o con un pty SI lo
        # ofrece. Y el codigo hay que escribirselo despues, asi que /dev/null no
        # vale. En Windows la tuberia funciona (y no hay pty).
        self._pty = None
        esclavo = None
        entrada = subprocess.PIPE
        if os.name != "nt":
            import pty
            self._pty, esclavo = pty.openpty()
            entrada = esclavo
        try:
            self.proceso = subprocess.Popen(
                cmd, stdin=entrada, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=motor.entorno_de({"home": self.carpeta}),
                **SIN_VENTANA)
        except OSError as fallo:
            self.estado = "fallo"
            self.mensaje = f"no se pudo ejecutar agy: {fallo}"
            self._cerrar_pty()
            return
        finally:
            if esclavo is not None:
                os.close(esclavo)
        threading.Thread(target=self._vigilar, daemon=True).start()
        threading.Thread(target=self._plazos, daemon=True).start()

    def _cerrar_pty(self):
        if getattr(self, "_pty", None) is not None:
            try:
                os.close(self._pty)
            except OSError:
                pass
            self._pty = None

    def _vigilar(self):
        proceso = self.proceso
        acumulado = ""
        while True:
            trozo = proceso.stdout.read1(4096) if hasattr(proceso.stdout, "read1") \
                else proceso.stdout.read(1)
            if not trozo:
                break
            acumulado += trozo.decode("utf-8", errors="replace")
            with self._lock:
                self._salida = acumulado
                if self.estado == "abriendo":
                    m = _ENLACE.search(acumulado)
                    if m:
                        self.enlace = m.group(0).rstrip(".,;)>'\"")
                        self.enlace_en = time.time()
                        self.estado = "enlace"
        proceso.wait()
        self._cerrar_pty()
        self._terminar(acumulado, proceso.returncode)

    def _terminar(self, salida, codigo):
        motor = _motor()
        payload = motor.leer_payload(salida)
        with self._lock:
            if self.estado == "fallo":                 # cancelado a mano
                return
            if codigo == 0 and payload.get("status") != "ERROR":
                self.estado = "dentro"
                self.mensaje = ""
                return
            self.estado = "fallo"
            if not self.enlace:
                # agy termino sin llegar a pedir el login: no es un plazo vencido
                ultima = [l for l in salida.strip().splitlines() if l.strip()][-1:] or [""]
                self.mensaje = ("agy terminó sin ofrecer ningún enlace de acceso"
                                + (f": {motor.limpio(payload.get('error') or ultima[0], 160)}"
                                   if (payload.get("error") or ultima[0]) else ""))
            elif self.codigo_en is None:
                self.mensaje = (f"se acabó la ventana de {ESPERA_ENLACE_S} s sin "
                                "recibir el código: pide otro enlace")
            else:
                detalle = payload.get("error") or salida.strip().splitlines()[-1:] or [""]
                if isinstance(detalle, list):
                    detalle = detalle[0]
                self.mensaje = ("Google no ha aceptado el código: "
                                + (motor.limpio(detalle, 160) or "pide otro enlace"))

    def _plazos(self):
        """Corta el proceso si se queda colgado mas de lo que agy promete."""
        while self.proceso.poll() is None:
            time.sleep(0.5)
            with self._lock:
                limite = ESPERA_CODIGO_S if self.codigo_en else ESPERA_ENLACE_S + 30
                base = self.codigo_en or self.arranque
                if time.time() - base > limite:
                    self._matar()
                    return

    # -- el codigo
    def escribir_codigo(self, codigo):
        codigo = str(codigo or "").strip()
        if not codigo:
            raise ErrorLogin("pega el código que te ha dado la página")
        with self._lock:
            if self.estado != "enlace":
                raise ErrorLogin(f"el acceso está en '{self.estado}': no espera "
                                 "ningún código ahora")
            if self.proceso is None or self.proceso.poll() is not None:
                raise ErrorLogin("agy ya no está esperando: pide otro enlace")
            self.estado = "probando"
            self.codigo_en = time.time()
            try:
                if getattr(self, "_pty", None) is not None:
                    # un terminal toma el Intro como \r, no como \n
                    os.write(self._pty, (codigo + "\r").encode("utf-8"))
                else:
                    self.proceso.stdin.write((codigo + "\n").encode("utf-8"))
                    self.proceso.stdin.flush()
            except OSError as fallo:
                self.estado = "fallo"
                self.mensaje = f"no se pudo enviar el código: {fallo}"
                raise ErrorLogin(self.mensaje) from fallo


# ------------------------------------------------------------------ API

def _barrer():
    for cid in [c for c, i in _INTENTOS.items() if i.caducado()]:
        _INTENTOS.pop(cid).cancelar()


def entrar(cuenta_id, carpeta):
    """Arranca (o reencuentra) el acceso de una cuenta. -> ficha del intento"""
    with _LOCK:
        _barrer()
        actual = _INTENTOS.get(cuenta_id)
        if actual is not None and actual.vivo():
            return actual.ver()
        if not instalado():
            raise ErrorLogin("no se encontró agy en este sistema")
        intento = Intento(cuenta_id, carpeta)
        _INTENTOS[cuenta_id] = intento
        intento.arrancar()
        return intento.ver()


def reiniciar(cuenta_id, carpeta):
    """Tira el intento vivo y arranca otro: el enlace caduca a los 60 s."""
    with _LOCK:
        viejo = _INTENTOS.pop(cuenta_id, None)
        if viejo is not None:
            viejo.cancelar()
    return entrar(cuenta_id, carpeta)


def pegar(cuenta_id, codigo):
    with _LOCK:
        intento = _INTENTOS.get(cuenta_id)
        if intento is None:
            raise ErrorLogin("no hay ningún acceso en marcha para esta cuenta")
    intento.escribir_codigo(codigo)
    # el codigo se comprueba en segundos: se le da un momento para contestar
    fin = time.time() + 20
    while time.time() < fin and intento.estado == "probando":
        time.sleep(0.25)
    return intento.ver()


def mirar(cuenta_id):
    """El intento en MEMORIA de esa cuenta, o None. No lanza nada."""
    with _LOCK:
        _barrer()
        intento = _INTENTOS.get(cuenta_id)
        return intento.ver() if intento is not None else None


def cancelar(cuenta_id):
    with _LOCK:
        intento = _INTENTOS.pop(cuenta_id, None)
    if intento is None:
        return False
    intento.cancelar()
    return True


def olvidar_todos():
    with _LOCK:
        intentos = list(_INTENTOS.values())
        _INTENTOS.clear()
    for intento in intentos:
        intento.cancelar()


def describir():
    return f"login de agy: {len(_INTENTOS)} intentos en memoria"


# ------------------------------------------------------------------ lo que ve la pantalla

def reconciliar():
    """Un login que termino DESPUES de que la peticion del codigo contestara.

    Pegar el codigo espera unos segundos, pero agy solo termina --y da el acceso
    por bueno-- cuando acaba su llamada de prueba. Si eso pasa cuando nadie
    esperaba, se encuentra aqui, en el intento que vive en memoria (`mirar` no
    lanza nada): la cuenta pasa a contar y queda apuntada como sana.
    """
    for cuenta in claves.cuentas_agy(solo_listas=False):
        intento = mirar(cuenta["id"])
        if intento and intento["estado"] == "dentro" and not cuenta["entrada"]:
            dar_por_dentro(cuenta["id"])


def dar_por_dentro(cuenta_id):
    """La cuenta ya tiene sesion: cuenta para el reparto y queda apuntada como sana."""
    motor = _motor()
    claves.apuntar_cuenta_agy(cuenta_id, entrada=True)
    with _LOCK:
        # el intento ya cumplio: si se quedara, la pantalla pintaria «dentro»
        # debajo de una cuenta que ya esta dentro
        _INTENTOS.pop(cuenta_id, None)
    motor.olvidar(cuenta_id)
    motor.anotar(cuenta_id, "ok", "sesión iniciada", para="entrar")


def cuentas_para_pantalla():
    """Las cuentas con su ultima salud apuntada. NO lanza agy: solo lee ficheros
    y el intento en memoria."""
    motor = _motor()
    ahora = time.time()
    salida = []
    for indice, cuenta in enumerate(claves.cuentas_agy(solo_listas=False)):
        salud = motor.salud_de(cuenta["id"])
        bloqueo = motor.bloqueo_de(salud, ahora)
        salida.append({
            "id": cuenta["id"],
            "etiqueta": cuenta["etiqueta"],
            "orden": indice,
            "manda": indice == 0,
            "activa": cuenta["activa"],
            "guardada": cuenta["entrada"],
            "intento": mirar(cuenta["id"]),
            "salud": salud,
            # segundos que le quedan apartada (cupo, saturacion), si lo esta
            "apartada_s": int(bloqueo[1] - ahora)
            if bloqueo and bloqueo[0] == "espera" else 0,
        })
    return salida


def estado_para_pantalla():
    """La respuesta de GET /api/claves/agy. Instantanea y sin gastar cupo."""
    reconciliar()
    motor = _motor()
    cuentas = cuentas_para_pantalla()
    return {
        "cuentas": cuentas,
        "max": claves.MAX_AGY,
        "instalado": motor.instalado(),
        "carpeta_base": carpeta_base(),
        # sin ninguna cuenta se usa la sesion por defecto de agy en la maquina
        "usa_defecto": not cuentas,
        "defecto": motor.salud_de(motor.DEFECTO),
    }
