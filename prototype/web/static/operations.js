/* operations.html: what the human team sees. Polls GET /api/operations and
   GET /api/audit every 5 s; "Reiniciar demo" sends POST /api/reset {}. */
"use strict";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;

/* Generic table: columns = [[title, (row) => content, tdClass?]] */
function table(rows, columns, emptyText) {
  if (!rows.length) return el("p", { class: "empty-table" }, emptyText);
  return el("table", {},
    el("thead", {}, el("tr", {}, columns.map(([t]) => el("th", { scope: "col" }, t)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, columns.map(([, fn, cls]) => el("td", { class: cls }, fn(r)))))));
}

// "2026-11-02T19:12:22.314386" -> "2026-11-02 19:12:22"
const shortDate = (iso) => (iso ? iso.replace("T", " ").slice(0, 19) : "—");
const who = (r) => `${r.name || ""} (${r.customer_id})`;

// Spanish display labels for backend enums.
const PROMISE_STATUS_LABELS = { active: "activa", kept: "cumplida", broken: "incumplida" };
const CAMPAIGN_STATUS_LABELS = { sent: "enviado", suppressed: "suprimido" };

function renderOperations(d) {
  renderCampaignHistory(d.campaigns || []);
  const cases = d.cases || [], promises = d.promises || [], plans = d.plans || [], links = d.links || [];
  $("kpi-cases").textContent = cases.length;
  $("kpi-promises").textContent = promises.length;
  $("kpi-plans").textContent = plans.length;
  $("kpi-links").textContent = links.length;

  $("table-cases").replaceChildren(table(cases, [
    ["Caso", (r) => r.case_id, "mono nowrap"],
    ["Cliente", who],
    ["Cola", (r) => r.queue],
    ["Motivo", (r) => r.reason],
    ["SLA", (r) => el("span", { class: r.sla_hours <= 2 ? "sla-urgent" : "" }, `${r.sla_hours} h`), "nowrap"],
    ["Resumen para el asesor", (r) => r.summary, "summary"],
    ["Creado", (r) => shortDate(r.created), "mono nowrap"],
  ], "Sin casos escalados todavía. Prueba los escenarios 3, 4 o 5 en el chat."));

  $("table-promises").replaceChildren(table(promises, [
    ["Cliente", who], ["Monto", (r) => usd(r.amount)], ["Fecha", (r) => r.date, "mono"],
    ["Estado", (r) => PROMISE_STATUS_LABELS[r.status] || r.status],
  ], "Sin promesas registradas."));

  $("table-plans").replaceChildren(table(plans, [
    ["Cliente", who], ["Total", (r) => usd(r.total)],
    ["Plan", (r) => (r.plan || []).map((i) => `${i.installment}) ${i.date}: ${usd(i.amount)}`).join(" · ")],
  ], "Sin acuerdos registrados."));

  $("table-links").replaceChildren(table(links, [
    ["Cliente", who], ["Monto", (r) => usd(r.amount)],
    // Fictitious URL (.example domain): shown as text, not as a link.
    ["URL (ficticia)", (r) => r.url, "mono"], ["Vence", (r) => r.expires_on, "mono"],
  ], "Sin links generados."));
}

/* Readable summary of an audit line (fields vary by event type). */
function auditSummary(e) {
  if (e.event === "tool") {
    const r = e.result || {};
    return `${e.name} → ${r.ok ? "ok" : (r.error?.code || "error")}`;
  }
  if (e.text) return e.decision ? `[${e.decision}] ${e.text}` : e.text;
  if (e.detail) return e.detail;
  const { ts, event, customer_id, ...rest } = e;
  return JSON.stringify(rest);
}

function renderAudit(events) {
  $("table-audit").replaceChildren(table(events, [
    ["Hora", (e) => shortDate(e.ts), "mono nowrap"],
    ["Cliente", (e) => e.customer_id, "mono"],
    ["Evento", (e) => e.event],
    ["Detalle", auditSummary, "summary"],
  ], "Sin eventos de auditoría."));
}

async function refresh() {
  try {
    const cid = $("customer-filter").value;
    const q = new URLSearchParams({ limit: "100", ...(cid ? { customer_id: cid } : {}) });
    const [ops, audit] = await Promise.all([API.get("/api/operations"), API.get(`/api/audit?${q}`)]);
    renderOperations(ops);
    renderAudit(audit.events || []);
    $("refresh-status").textContent = `Actualizado ${new Date().toLocaleTimeString("es-PA")}`;
  } catch (e) {
    $("refresh-status").textContent = e.message === "unauthorized" ? "Falta el token" : `Sin conexión (${e.message})`;
  }
}

async function loadFilter() {
  try {
    const { customers } = await API.get("/api/customers");
    $("customer-filter").replaceChildren(el("option", { value: "" }, "Todos"),
      ...customers.map((c) => el("option", { value: c.customer_id }, `${c.customer_id} · ${c.name}`)));
  } catch { /* the filter stays on "Todos" */ }
}

/* Inline confirmation (no browser alert/confirm). */
function askConfirmation() {
  const zone = $("reset-zone");
  const cancel = el("button", { class: "btn", type: "button", onclick: restoreButton }, "Cancelar");
  zone.replaceChildren(
    el("span", { role: "status" }, "¿Borrar sesiones, promesas, acuerdos, links y casos?"),
    el("button", { class: "btn btn-danger", type: "button", onclick: resetDemo }, "Sí, reiniciar"),
    cancel);
  cancel.focus();
}

function restoreButton() {
  const btn = el("button", { id: "btn-reset-demo", class: "btn", type: "button", onclick: askConfirmation }, "Reiniciar demo");
  $("reset-zone").replaceChildren(btn);
  btn.focus();
}

async function resetDemo() {
  try {
    await API.post("/api/reset", {});
    $("reset-zone").replaceChildren(el("span", { role: "status" }, "Demo reiniciada ✓"));
    setTimeout(restoreButton, 1500);
    refresh();
  } catch (e) {
    $("reset-zone").replaceChildren(el("span", { role: "alert", class: "sla-urgent" }, `Error: ${e.message}`));
    setTimeout(restoreButton, 2500);
  }
}

// ---------------------------------------------------------------- campaigns
/* Backend suppression codes -> sentence for the committee / collections team. */
const REASONS = {
  no_opt_in: "Sin consentimiento (opt-in) para mensajes proactivos",
  opt_out: "Pidió no recibir más avisos (respondió BAJA)",
  outside_hours: "Fuera del horario permitido (8:00–19:00, lun–sáb, hora de Panamá)",
  open_claim: "Tiene un reclamo abierto: no se cobra durante una disputa",
  non_collectable_segment: "Cuenta de alto valor / B2B: la gestiona un ejecutivo humano",
  open_human_case: "Ya tiene un caso abierto con un asesor",
  active_promise: "Tiene una promesa de pago vigente",
  recently_contacted: "Ya fue contactado hace menos de 3 días",
  no_balance: "No tiene saldo vencido",
};
const readableReason = (m) => REASONS[m] || m || "—";
const SHORT_REASONS = {
  no_opt_in: "sin opt-in", opt_out: "opt-out (BAJA)", outside_hours: "fuera de horario",
  open_claim: "reclamo abierto", non_collectable_segment: "B2B → ejecutivo", open_human_case: "caso con asesor",
  active_promise: "promesa vigente", recently_contacted: "contactado < 3 días", no_balance: "sin saldo",
};
const SEGMENT_NAMES = {};

async function loadCampaigns() {
  try {
    const { segments = [], template } = await API.get("/api/campaigns/segments");
    segments.forEach((s) => (SEGMENT_NAMES[s.id] = s.name));
    $("segment-list").replaceChildren(...segments.map((s) => el("label", { class: "segment" },
      el("input", { type: "checkbox", name: "segment", value: s.id, checked: s.id !== "high_value_b2b" }),
      el("span", {}, s.name),
      el("span", { class: "muted segment-count" }, `${s.estimated_eligible} elegible${s.estimated_eligible === 1 ? "" : "s"}`))));
    renderTemplate(template);
    $("btn-launch").disabled = false;
  } catch (e) {
    $("segment-list").replaceChildren(el("span", { class: "muted" },
      e.message === "unauthorized" ? "Falta el token." : `Campañas no disponibles (${e.message}).`));
    $("btn-launch").disabled = true;
  }
}

/* Template text with {{1}} highlighted as a variable (no innerHTML). */
function renderTemplate(t) {
  if (!t) return $("template-preview").replaceChildren();
  const parts = String(t.text || "").split(/(\{\{\d+\}\})/);
  $("template-preview").replaceChildren(
    el("div", { class: "template-header" },
      el("span", { class: "mono" }, t.name),
      el("span", { class: "badge badge-utility" }, t.category || "UTILITY")),
    el("p", { class: "template-text" },
      parts.map((p) => (/^\{\{\d+\}\}$/.test(p) ? el("span", { class: "variable", title: "Variable: nombre del cliente" }, p) : p))),
    el("p", { class: "template-note" }, "✔ Plantilla aprobada por Meta · no incluye montos: la deuda solo se muestra tras verificar identidad."));
}

async function launchCampaign(ev) {
  ev.preventDefault();
  const segments = [...document.querySelectorAll("input[name=segment]:checked")].map((i) => i.value);
  if (!segments.length) {
    $("campaign-status").textContent = "Elige al menos un segmento.";
    return;
  }
  const force = $("force-hours").checked;
  $("btn-launch").disabled = true;
  $("campaign-status").textContent = "Enviando…";
  try {
    const r = await API.post("/api/campaigns/run", { segments, ...(force ? { force_hours: true } : {}) });
    renderResult(r, force);
    $("campaign-status").textContent = "";
    refresh();
  } catch (e) {
    $("campaign-status").textContent = `Error: ${e.message}`;
  } finally {
    $("btn-launch").disabled = false;
  }
}

function renderResult(r, forced) {
  const results = r.results || [];
  const summary = r.summary || {};
  const sent = summary.sent ?? results.filter((x) => x.status === "sent").length;
  const suppressed = summary.suppressed ?? results.filter((x) => x.status !== "sent").length;
  $("campaign-result").replaceChildren(
    el("div", { class: "turn-header result-header" },
      el("strong", { class: "mono" }, r.campaign_id || "Campaña"),
      el("span", { class: "muted" }, `${shortDate(r.executed_at)} · plantilla ${r.template || "—"}`),
      forced && el("span", { class: "badge badge-forced" }, "Horario ignorado (demo)")),
    el("div", { class: "kpis" },
      el("div", { class: "kpi kpi-ok" }, el("div", { class: "kpi-value" }, sent), el("div", { class: "kpi-label" }, "Enviados")),
      el("div", { class: "kpi kpi-suppressed" }, el("div", { class: "kpi-value" }, suppressed), el("div", { class: "kpi-label" }, "Suprimidos por regla"))),
    el("div", { class: "table-wrap" }, table(results, [
      ["Cliente", who],
      ["Segmento", (x) => SEGMENT_NAMES[x.segment] || x.segment],
      ["Estado", (x) => el("span", { class: `badge ${x.status === "sent" ? "badge-sent" : "badge-suppressed"}` },
        CAMPAIGN_STATUS_LABELS[x.status] || x.status), "nowrap"],
      ["Motivo", (x) => (x.status === "sent"
        ? el("span", { class: "muted" }, "Plantilla entregada; espera respuesta del cliente")
        : el("span", {}, readableReason(x.reason), x.detail && x.detail !== readableReason(x.reason) && el("div", { class: "muted reason-detail" }, x.detail))), "summary"],
    ], "La campaña no encontró clientes en esos segmentos.")));
}

function reasonSummary(byReason = {}) {
  const e = Object.entries(byReason);
  return e.length ? e.map(([k, n]) => `${n} × ${SHORT_REASONS[k] || k}`).join(" · ") : "—";
}

function renderCampaignHistory(campaigns) {
  $("table-campaigns").replaceChildren(table(campaigns, [
    ["Campaña", (c) => c.campaign_id, "mono nowrap"],
    ["Ejecutada", (c) => shortDate(c.executed_at), "mono nowrap"],
    ["Enviados", (c) => c.summary?.sent ?? "—"],
    ["Suprimidos", (c) => c.summary?.suppressed ?? "—"],
    ["Por motivo", (c) => reasonSummary(c.summary?.by_reason), "summary"],
  ], "Aún no se ha lanzado ninguna campaña."));
}

$("campaign-form").addEventListener("submit", launchCampaign);
$("btn-reset-demo").addEventListener("click", askConfirmation);
$("customer-filter").addEventListener("change", refresh);
mountTokenBanner(() => { loadFilter(); loadCampaigns(); refresh(); });
loadFilter();
loadCampaigns();
refresh();
setInterval(refresh, REFRESH_MS);
