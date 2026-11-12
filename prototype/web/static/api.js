/* HTTP client shared by index.html and operations.html.
   - Demo token: arrives once as ?t=<token>, is stored in localStorage and
     travels in the x-demo-token header (validated by agil/api.py).
   - If the API answers 401, a field to paste the token is shown.
   - Helper `el()`: builds nodes with textContent; we never use innerHTML with data. */
"use strict";

const API = (() => {
  const STORAGE_KEY = "agil_demo_token";

  function readToken() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; } catch { return ""; }
  }
  function saveToken(t) {
    try { localStorage.setItem(STORAGE_KEY, t); } catch { /* private mode: memory only */ }
    memory = t;
  }

  // First visit with ?t=...: store it and strip it from the address bar.
  let memory = readToken();
  const fromUrl = new URLSearchParams(location.search).get("t");
  if (fromUrl) {
    saveToken(fromUrl);
    history.replaceState(null, "", location.pathname);
  }

  let onUnauthorizedFn = () => {};

  async function request(path, options = {}) {
    const headers = { "content-type": "application/json" };
    if (memory) headers["x-demo-token"] = memory;
    const r = await fetch(path, { ...options, headers });
    if (r.status === 401) {
      onUnauthorizedFn();
      throw new Error("unauthorized");
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  return {
    get: (path) => request(path),
    post: (path, body) => request(path, { method: "POST", body: JSON.stringify(body) }),
    saveToken,
    set onUnauthorized(fn) { onUnauthorizedFn = fn; },
  };
})();

/* el("div", {class: "x"}, "text", otherNode) -> node. Strings are inserted
   as text nodes, so any server data stays escaped. */
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

const usd = (n) => (typeof n === "number" ? `USD ${n.toFixed(2)}` : "—");

/* Token banner: inserted at the top of the page when the API returns 401. */
function mountTokenBanner(onSave) {
  API.onUnauthorized = () => {
    if (document.getElementById("token-banner")) return;
    const input = el("input", { type: "password", id: "token-input", autocomplete: "off",
      placeholder: "Pega aquí el token de la demo", "aria-label": "Token de la demo" });
    const form = el("form", { class: "token-banner", id: "token-banner", role: "alert",
      onsubmit: (e) => {
        e.preventDefault();
        if (!input.value.trim()) return;
        API.saveToken(input.value.trim());
        form.remove();
        onSave();
      } },
      el("span", {}, "Esta demo requiere un token de acceso."),
      input,
      el("button", { type: "submit", class: "btn btn-primary" }, "Guardar"));
    document.body.prepend(form);
    input.focus();
  };
}
