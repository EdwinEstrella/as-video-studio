/* Studio Videos IA — logica del formulario de acceso.
   Correo y contraseña. Sin ninguna cuenta, el mismo formulario la crea
   (`primeraVez` de /api/csrf → /api/alta), como la primera vez de n8n. */
(function () {
  'use strict';

  const form       = document.getElementById('loginForm');
  const email      = document.getElementById('email');
  const password   = document.getElementById('password');
  const repetir    = document.getElementById('repetir');
  const campoRepetir = document.getElementById('campoRepetir');
  const titulo     = document.getElementById('titulo');
  const subtitulo  = document.getElementById('subtitulo');
  const recuperacion = document.getElementById('recuperacion');
  const submit     = document.getElementById('submit');
  const submitText = document.getElementById('submitText');
  const errorBox   = document.getElementById('error');
  const errorText  = document.getElementById('errorText');
  const reveal     = document.getElementById('reveal');
  const eyeOpen    = document.getElementById('eyeOpen');
  const eyeClosed  = document.getElementById('eyeClosed');
  const panelLink  = document.getElementById('panelLink');

  let csrfToken  = null;
  let saliendo   = false;
  let primeraVez = false;

  /* --- la primera vez: crear la cuenta ------------------------------------ */
  /* Sin cuentas no hay nada que recuperar, así que la ayuda de la contraseña
     olvidada se esconde; y se pide repetirla, que no hay otra forma de saber
     que se tecleó la que se quería. */
  function modoAlta(activo) {
    primeraVez = activo;
    titulo.textContent = activo ? 'Crea tu cuenta' : 'Inicia sesión';
    subtitulo.textContent = activo
      ? 'Primera vez en AS Video Studio: con este correo y esta contraseña entrarás a partir de ahora.'
      : 'AS Video Studio';
    campoRepetir.classList.toggle('is-hidden', !activo);
    if (recuperacion) recuperacion.classList.toggle('is-hidden', activo);
    password.autocomplete = activo ? 'new-password' : 'current-password';
    setLoading(false);
  }

  /* --- por donde se ha entrado -------------------------------------------- */
  /* Hay dos versiones del estudio en el mismo dominio (la 1 en la raiz, la 2 en
     /v2/) y un solo login. Lo que dice a cual se vuelve es la direccion por la
     que se llego: quien pide algo de /v2/ acaba en /v2/login, y de ahi se
     vuelve a /v2/. nginx puede ademas apuntar la pagina exacta en `?next=`.
     El servidor no se lo cree sin mirar: valida la ruta antes de devolverla. */
  function destino() {
    const pedido = new URLSearchParams(window.location.search).get('next');
    if (pedido) return pedido;
    return window.location.pathname.startsWith('/v2/') ? '/v2/' : '';
  }

  /* Y se dice cual es, que si no el login de la v2 no se distingue del de la v1. */
  (function marcarVersion() {
    if (!window.location.pathname.startsWith('/v2/')) return;
    document.title = 'Acceso · AS Video Studio';
    const sub = document.querySelector('.card__sub');
    if (sub) sub.textContent = 'AS Video Studio';
  })();

  /* --- token anti-CSRF: se pide al cargar y se refresca si caduca ---------- */
  /* Con el token viaja el enlace al VPS en el panel de Hostinger, para el «he
     olvidado la contraseña»: el id del VPS lo sabe el servidor (.env), no esta
     pagina. Sin id se queda el enlace al listado de VPS, que tambien sirve. */
  async function fetchCsrf() {
    try {
      const res  = await fetch('/api/csrf', { credentials: 'same-origin' });
      const data = await res.json();
      csrfToken = data.csrfToken || null;
      if (data.primeraVez !== primeraVez) modoAlta(!!data.primeraVez);
      const panel = (data.recuperacion || {}).panel;
      if (panelLink && panel && /^https:\/\/hpanel\.hostinger\.com\//.test(panel)) {
        panelLink.href = panel;
      }
    } catch {
      csrfToken = null;
    }
    return csrfToken;
  }
  fetchCsrf();

  /* --- avisos ------------------------------------------------------------- */
  function showError(message) {
    errorText.textContent = message;
    errorBox.classList.add('is-visible');
  }
  function clearError() {
    errorBox.classList.remove('is-visible');
  }

  /* --- estado del boton --------------------------------------------------- */
  function setLoading(loading) {
    submit.disabled = loading;
    password.disabled = loading;
    email.disabled = loading;
    repetir.disabled = loading;
    submitText.textContent = loading
      ? (primeraVez ? 'Creando…' : 'Comprobando…')
      : (primeraVez ? 'Crear cuenta' : 'Entrar');

    const old = submit.querySelector('.spinner');
    if (old) old.remove();
    if (loading) {
      const sp = document.createElement('span');
      sp.className = 'spinner';
      submit.prepend(sp);
    }
  }

  /* --- mostrar / ocultar contraseña --------------------------------------- */
  reveal.addEventListener('click', function () {
    const isText = password.type === 'text';
    password.type = isText ? 'password' : 'text';
    eyeOpen.classList.toggle('is-hidden', !isText);
    eyeClosed.classList.toggle('is-hidden', isText);
    reveal.setAttribute('aria-pressed', String(!isText));
    reveal.setAttribute('aria-label', isText ? 'Mostrar contraseña' : 'Ocultar contraseña');
    password.focus();
  });

  password.addEventListener('input', clearError);
  email.addEventListener('input', clearError);
  repetir.addEventListener('input', clearError);

  /* --- envío -------------------------------------------------------------- */
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    clearError();

    const pass   = password.value;
    const correo = email.value.trim();

    if (!correo) {
      showError('Escribe tu correo.');
      email.focus();
      return;
    }
    if (!pass) {
      showError('Escribe tu contraseña.');
      password.focus();
      return;
    }
    // El mismo minimo que MIN_PASSWORD de lib/auth.js; el servidor lo vuelve a mirar.
    if (primeraVez && pass.length < 8) {
      showError('La contraseña necesita al menos 8 caracteres.');
      password.focus();
      return;
    }
    if (primeraVez && pass !== repetir.value) {
      showError('Las dos contraseñas no coinciden.');
      repetir.focus();
      return;
    }

    setLoading(true);

    try {
      if (!csrfToken) await fetchCsrf();

      let res = await send(correo, pass);

      // Si el token había caducado (sesión reiniciada), lo renovamos y reintentamos una vez.
      if (res.status === 403) {
        await fetchCsrf();
        res = await send(correo, pass);
      }

      const data = await res.json().catch(() => ({}));

      if (res.ok && data.ok) {
        saliendo = true;
        submitText.textContent = 'Entrando…';
        // Redirección dura: que la página la sirva el servidor con la sesión ya
        // activa. A dónde, lo dice él (ver `destinoTrasLogin`).
        window.location.assign(data.redirect || '/studio');
        return;
      }

      // 409: alguien creó la cuenta mientras tanto. Se vuelve al login normal.
      if (res.status === 409) modoAlta(false);
      showError(data.error || 'No se ha podido iniciar sesión. Inténtalo de nuevo.');
      if (!primeraVez) {
        password.value = '';
        password.focus();
      }
    } catch {
      showError('No hay conexión con el servidor. Comprueba tu red e inténtalo de nuevo.');
    } finally {
      // Mientras el navegador va a la página siguiente el botón se queda como
      // está: volver a habilitarlo sólo deja un parpadeo de «Entrar» encima de
      // una pantalla que ya se ha ido.
      if (!saliendo) setLoading(false);
    }
  });

  function send(correo, pass) {
    return fetch(primeraVez ? '/api/alta' : '/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken || '',
      },
      body: JSON.stringify({ email: correo, password: pass, next: destino() }),
    });
  }
})();
