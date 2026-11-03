/* Cliente HTTP compartido por index.html y operacion.html.
   - Token de demo: llega una vez como ?t=<token>, se guarda en localStorage y
     viaja en el header x-demo-token (lo valida agil/api.py).
   - Si la API responde 401 se muestra un campo para pegar el token.
   - Helper `el()`: crea nodos con textContent; nunca usamos innerHTML con datos. */
"use strict";

const API = (() => {
  const CLAVE = "agil_demo_token";

  function leerToken() {
    try { return localStorage.getItem(CLAVE) || ""; } catch { return ""; }
  }
  function guardarToken(t) {
    try { localStorage.setItem(CLAVE, t); } catch { /* modo privado: solo memoria */ }
    memoria = t;
  }

  // Primera visita con ?t=...: lo guardamos y lo quitamos de la barra de direcciones.
  let memoria = leerToken();
  const deUrl = new URLSearchParams(location.search).get("t");
  if (deUrl) {
    guardarToken(deUrl);
    history.replaceState(null, "", location.pathname);
  }

  let alNoAutorizado = () => {};

  async function pedir(ruta, opciones = {}) {
    const headers = { "content-type": "application/json" };
    if (memoria) headers["x-demo-token"] = memoria;
    const r = await fetch(ruta, { ...opciones, headers });
    if (r.status === 401) {
      alNoAutorizado();
      throw new Error("no_autorizado");
    }
    const datos = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(datos.error || `HTTP ${r.status}`);
    return datos;
  }

  return {
    get: (ruta) => pedir(ruta),
    post: (ruta, cuerpo) => pedir(ruta, { method: "POST", body: JSON.stringify(cuerpo) }),
    guardarToken,
    set onNoAutorizado(fn) { alNoAutorizado = fn; },
  };
})();

/* el("div", {class: "x"}, "texto", otroNodo) -> nodo. Los strings se insertan
   como nodos de texto, así que cualquier dato del servidor queda escapado. */
function el(tag, attrs = {}, ...hijos) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const h of hijos.flat()) {
    if (h === null || h === undefined || h === false) continue;
    n.append(h instanceof Node ? h : document.createTextNode(String(h)));
  }
  return n;
}

const usd = (n) => (typeof n === "number" ? `USD ${n.toFixed(2)}` : "—");

/* Banner de token: se inserta arriba de la página cuando la API devuelve 401. */
function montarAvisoToken(alGuardar) {
  API.onNoAutorizado = () => {
    if (document.getElementById("aviso-token")) return;
    const input = el("input", { type: "password", id: "token-input", autocomplete: "off",
      placeholder: "Pega aquí el token de la demo", "aria-label": "Token de la demo" });
    const form = el("form", { class: "aviso-token", id: "aviso-token", role: "alert",
      onsubmit: (e) => {
        e.preventDefault();
        if (!input.value.trim()) return;
        API.guardarToken(input.value.trim());
        form.remove();
        alGuardar();
      } },
      el("span", {}, "Esta demo requiere un token de acceso."),
      input,
      el("button", { type: "submit", class: "btn btn-primario" }, "Guardar"));
    document.body.prepend(form);
    input.focus();
  };
}
