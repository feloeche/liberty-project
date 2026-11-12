/* operacion.html: lo que ve el equipo humano. Lee GET /api/operacion y
   GET /api/auditoria cada 5 s; "Reiniciar demo" hace POST /api/reiniciar {}. */
"use strict";

const $ = (id) => document.getElementById(id);
const REFRESCO_MS = 5000;

/* Tabla genérica: columnas = [[titulo, (fila) => contenido, claseTd?]] */
function tabla(filas, columnas, vacio) {
  if (!filas.length) return el("p", { class: "vacia" }, vacio);
  return el("table", {},
    el("thead", {}, el("tr", {}, columnas.map(([t]) => el("th", { scope: "col" }, t)))),
    el("tbody", {}, filas.map((f) => el("tr", {}, columnas.map(([, fn, cls]) => el("td", { class: cls }, fn(f)))))));
}

// "2026-11-02T19:12:22.314386" -> "2026-11-02 19:12:22"
const fechaCorta = (iso) => (iso ? iso.replace("T", " ").slice(0, 19) : "—");
const quien = (f) => `${f.nombre || ""} (${f.cliente_id})`;

function pintarOperacion(d) {
  pintarHistorialCampanas(d.campanas || []);
  const casos = d.casos || [], promesas = d.promesas || [], acuerdos = d.acuerdos || [], links = d.links || [];
  $("kpi-casos").textContent = casos.length;
  $("kpi-promesas").textContent = promesas.length;
  $("kpi-acuerdos").textContent = acuerdos.length;
  $("kpi-links").textContent = links.length;

  $("tabla-casos").replaceChildren(tabla(casos, [
    ["Caso", (f) => f.caso_id, "mono nowrap"],
    ["Cliente", quien],
    ["Cola", (f) => f.cola],
    ["Motivo", (f) => f.motivo],
    ["SLA", (f) => el("span", { class: f.sla_horas <= 2 ? "sla-urgente" : "" }, `${f.sla_horas} h`), "nowrap"],
    ["Resumen para el asesor", (f) => f.resumen, "resumen"],
    ["Creado", (f) => fechaCorta(f.creado), "mono nowrap"],
  ], "Sin casos escalados todavía. Prueba los escenarios 3, 4 o 5 en el chat."));

  $("tabla-promesas").replaceChildren(tabla(promesas, [
    ["Cliente", quien], ["Monto", (f) => usd(f.monto)], ["Fecha", (f) => f.fecha, "mono"], ["Estado", (f) => f.estado],
  ], "Sin promesas registradas."));

  $("tabla-acuerdos").replaceChildren(tabla(acuerdos, [
    ["Cliente", quien], ["Total", (f) => usd(f.total)],
    ["Plan", (f) => (f.plan || []).map((c) => `${c.cuota}) ${c.fecha}: ${usd(c.monto)}`).join(" · ")],
  ], "Sin acuerdos registrados."));

  $("tabla-links").replaceChildren(tabla(links, [
    ["Cliente", quien], ["Monto", (f) => usd(f.monto)],
    // URL ficticia (dominio .example): se muestra como texto, no como enlace.
    ["URL (ficticia)", (f) => f.url, "mono"], ["Vence", (f) => f.vence, "mono"],
  ], "Sin links generados."));
}

/* Resumen legible de una línea de auditoría (los campos varían por tipo de evento). */
function resumenAuditoria(e) {
  if (e.evento === "tool") {
    const r = e.resultado || {};
    return `${e.nombre} → ${r.ok ? "ok" : (r.error?.codigo || "error")}`;
  }
  if (e.texto) return e.decision ? `[${e.decision}] ${e.texto}` : e.texto;
  if (e.detalle) return e.detalle;
  const { ts, evento, cliente_id, ...resto } = e;
  return JSON.stringify(resto);
}

function pintarAuditoria(eventos) {
  $("tabla-auditoria").replaceChildren(tabla(eventos, [
    ["Hora", (e) => fechaCorta(e.ts), "mono nowrap"],
    ["Cliente", (e) => e.cliente_id, "mono"],
    ["Evento", (e) => e.evento],
    ["Detalle", resumenAuditoria, "resumen"],
  ], "Sin eventos de auditoría."));
}

async function refrescar() {
  try {
    const cid = $("filtro-cliente").value;
    const q = new URLSearchParams({ limite: "100", ...(cid ? { cliente_id: cid } : {}) });
    const [op, aud] = await Promise.all([API.get("/api/operacion"), API.get(`/api/auditoria?${q}`)]);
    pintarOperacion(op);
    pintarAuditoria(aud.eventos || []);
    $("estado-refresh").textContent = `Actualizado ${new Date().toLocaleTimeString("es-PA")}`;
  } catch (e) {
    $("estado-refresh").textContent = e.message === "no_autorizado" ? "Falta el token" : `Sin conexión (${e.message})`;
  }
}

async function cargarFiltro() {
  try {
    const { clientes } = await API.get("/api/clientes");
    $("filtro-cliente").replaceChildren(el("option", { value: "" }, "Todos"),
      ...clientes.map((c) => el("option", { value: c.cliente_id }, `${c.cliente_id} · ${c.nombre}`)));
  } catch { /* el filtro queda en "Todos" */ }
}

/* Confirmación inline (sin alert/confirm del navegador). */
function pedirConfirmacion() {
  const zona = $("zona-reinicio");
  const cancelar = el("button", { class: "btn", type: "button", onclick: restaurarBoton }, "Cancelar");
  zona.replaceChildren(
    el("span", { role: "status" }, "¿Borrar sesiones, promesas, acuerdos, links y casos?"),
    el("button", { class: "btn btn-peligro", type: "button", onclick: reiniciarDemo }, "Sí, reiniciar"),
    cancelar);
  cancelar.focus();
}

function restaurarBoton() {
  const btn = el("button", { id: "btn-reiniciar-demo", class: "btn", type: "button", onclick: pedirConfirmacion }, "Reiniciar demo");
  $("zona-reinicio").replaceChildren(btn);
  btn.focus();
}

async function reiniciarDemo() {
  try {
    await API.post("/api/reiniciar", {});
    $("zona-reinicio").replaceChildren(el("span", { role: "status" }, "Demo reiniciada ✓"));
    setTimeout(restaurarBoton, 1500);
    refrescar();
  } catch (e) {
    $("zona-reinicio").replaceChildren(el("span", { role: "alert", class: "sla-urgente" }, `Error: ${e.message}`));
    setTimeout(restaurarBoton, 2500);
  }
}

// ---------------------------------------------------------------- campañas
/* Códigos de supresión del backend -> frase para el comité / el equipo de cobranza. */
const MOTIVOS = {
  sin_opt_in: "Sin consentimiento (opt-in) para mensajes proactivos",
  opt_out: "Pidió no recibir más avisos (respondió BAJA)",
  fuera_de_horario: "Fuera del horario permitido (8:00–19:00, lun–sáb, hora de Panamá)",
  reclamo_abierto: "Tiene un reclamo abierto: no se cobra durante una disputa",
  segmento_no_cobrable: "Cuenta de alto valor / B2B: la gestiona un ejecutivo humano",
  caso_humano_abierto: "Ya tiene un caso abierto con un asesor",
  promesa_vigente: "Tiene una promesa de pago vigente",
  contactado_recientemente: "Ya fue contactado hace menos de 3 días",
  sin_saldo: "No tiene saldo vencido",
};
const motivoLegible = (m) => MOTIVOS[m] || m || "—";
const MOTIVOS_CORTOS = {
  sin_opt_in: "sin opt-in", opt_out: "opt-out (BAJA)", fuera_de_horario: "fuera de horario",
  reclamo_abierto: "reclamo abierto", segmento_no_cobrable: "B2B → ejecutivo", caso_humano_abierto: "caso con asesor",
  promesa_vigente: "promesa vigente", contactado_recientemente: "contactado < 3 días", sin_saldo: "sin saldo",
};
const NOMBRE_SEG = {};

async function cargarCampanas() {
  try {
    const { segmentos = [], plantilla } = await API.get("/api/campanas/segmentos");
    segmentos.forEach((s) => (NOMBRE_SEG[s.id] = s.nombre));
    $("lista-segmentos").replaceChildren(...segmentos.map((s) => el("label", { class: "segmento" },
      el("input", { type: "checkbox", name: "segmento", value: s.id, checked: s.id !== "alto_valor_b2b" }),
      el("span", {}, s.nombre),
      el("span", { class: "tenue segmento-n" }, `${s.elegibles_estimados} elegible${s.elegibles_estimados === 1 ? "" : "s"}`))));
    pintarPlantilla(plantilla);
    $("btn-lanzar").disabled = false;
  } catch (e) {
    $("lista-segmentos").replaceChildren(el("span", { class: "tenue" },
      e.message === "no_autorizado" ? "Falta el token." : `Campañas no disponibles (${e.message}).`));
    $("btn-lanzar").disabled = true;
  }
}

/* Texto de la plantilla con {{1}} resaltado como variable (sin innerHTML). */
function pintarPlantilla(p) {
  if (!p) return $("plantilla-preview").replaceChildren();
  const partes = String(p.texto || "").split(/(\{\{\d+\}\})/);
  $("plantilla-preview").replaceChildren(
    el("div", { class: "plantilla-cab" },
      el("span", { class: "mono" }, p.nombre),
      el("span", { class: "badge badge-utility" }, p.categoria || "UTILITY")),
    el("p", { class: "plantilla-texto" },
      partes.map((t) => (/^\{\{\d+\}\}$/.test(t) ? el("span", { class: "variable", title: "Variable: nombre del cliente" }, t) : t))),
    el("p", { class: "plantilla-nota" }, "✔ Plantilla aprobada por Meta · no incluye montos: la deuda solo se muestra tras verificar identidad."));
}

async function lanzarCampana(ev) {
  ev.preventDefault();
  const segmentos = [...document.querySelectorAll("input[name=segmento]:checked")].map((i) => i.value);
  if (!segmentos.length) {
    $("estado-campana").textContent = "Elige al menos un segmento.";
    return;
  }
  const forzar = $("forzar-horario").checked;
  $("btn-lanzar").disabled = true;
  $("estado-campana").textContent = "Enviando…";
  try {
    const r = await API.post("/api/campanas/ejecutar", { segmentos, ...(forzar ? { forzar_horario: true } : {}) });
    pintarResultado(r, forzar);
    $("estado-campana").textContent = "";
    refrescar();
  } catch (e) {
    $("estado-campana").textContent = `Error: ${e.message}`;
  } finally {
    $("btn-lanzar").disabled = false;
  }
}

function pintarResultado(r, forzar) {
  const res = r.resultados || [];
  const resumen = r.resumen || {};
  const enviados = resumen.enviados ?? res.filter((x) => x.estado === "enviado").length;
  const suprimidos = resumen.suprimidos ?? res.filter((x) => x.estado !== "enviado").length;
  $("resultado-campana").replaceChildren(
    el("div", { class: "turno-cab resultado-cab" },
      el("strong", { class: "mono" }, r.campana_id || "Campaña"),
      el("span", { class: "tenue" }, `${fechaCorta(r.ejecutada)} · plantilla ${r.plantilla || "—"}`),
      forzar && el("span", { class: "badge badge-forzado" }, "Horario ignorado (demo)")),
    el("div", { class: "kpis" },
      el("div", { class: "kpi kpi-ok" }, el("div", { class: "kpi-valor" }, enviados), el("div", { class: "kpi-label" }, "Enviados")),
      el("div", { class: "kpi kpi-sup" }, el("div", { class: "kpi-valor" }, suprimidos), el("div", { class: "kpi-label" }, "Suprimidos por regla"))),
    el("div", { class: "tabla-wrap" }, tabla(res, [
      ["Cliente", quien],
      ["Segmento", (f) => NOMBRE_SEG[f.segmento] || f.segmento],
      ["Estado", (f) => el("span", { class: `badge ${f.estado === "enviado" ? "badge-enviado" : "badge-suprimido"}` },
        f.estado === "enviado" ? "enviado" : "suprimido"), "nowrap"],
      ["Motivo", (f) => (f.estado === "enviado"
        ? el("span", { class: "tenue" }, "Plantilla entregada; espera respuesta del cliente")
        : el("span", {}, motivoLegible(f.motivo), f.detalle && f.detalle !== motivoLegible(f.motivo) && el("div", { class: "tenue detalle-motivo" }, f.detalle))), "resumen"],
    ], "La campaña no encontró clientes en esos segmentos.")));
}

function resumenMotivos(pm = {}) {
  const e = Object.entries(pm);
  return e.length ? e.map(([k, n]) => `${n} × ${MOTIVOS_CORTOS[k] || k}`).join(" · ") : "—";
}

function pintarHistorialCampanas(campanas) {
  $("tabla-campanas").replaceChildren(tabla(campanas, [
    ["Campaña", (f) => f.campana_id, "mono nowrap"],
    ["Ejecutada", (f) => fechaCorta(f.ejecutada), "mono nowrap"],
    ["Enviados", (f) => f.resumen?.enviados ?? "—"],
    ["Suprimidos", (f) => f.resumen?.suprimidos ?? "—"],
    ["Por motivo", (f) => resumenMotivos(f.resumen?.por_motivo), "resumen"],
  ], "Aún no se ha lanzado ninguna campaña."));
}

$("form-campana").addEventListener("submit", lanzarCampana);
$("btn-reiniciar-demo").addEventListener("click", pedirConfirmacion);
$("filtro-cliente").addEventListener("change", refrescar);
montarAvisoToken(() => { cargarFiltro(); cargarCampanas(); refrescar(); });
cargarFiltro();
cargarCampanas();
refrescar();
setInterval(refrescar, REFRESCO_MS);
