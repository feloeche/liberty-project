"""Demo HTTP API (static web + /api/*), no frameworks.

The core is `handle()`, a pure function (method, path, query, headers, body)
-> (status, headers, body). On top of it sit two thin adapters:
  - lambda_handler: API Gateway HTTP API (payload v2) -> handle()
  - serve:          stdlib http.server for `python -m agil serve`
That way local and AWS run exactly the same code, and FastAPI/Flask is not
packaged into the Lambda (fewer dependencies, shorter cold start)."""
import base64
import hmac
import json
import mimetypes
import os
import time
import traceback
from functools import cache

from . import campaigns, config
from .agent import Agent, Session
from .llm import create_llm
from .store import create_persistence, load_customers, read_audit
from .tools import balance_of, segment_of

MAX_TEXT = 1000
PAGES = {"/": "index.html", "/operations": "operations.html"}
# Suggested scenario label per customer (same as the CLI demo). Spanish: shown in the UI.
SCENARIOS = {"C001": "Olvidadizo: link de pago", "C002": "Dificultad temporal: promesa ajustada",
             "C003": "Reincidente: pide 6 cuotas y escala", "C004": "En disputa: escala sin cobrar",
             "C005": "B2B: escala a un ejecutivo", "C006": "Acuerdo en 3 cuotas",
             "C007": "Vulnerable: escala con prioridad"}
OPERATIONS_TABLES = {"cases": "escalations", "promises": "promises", "plans": "plans", "links": "links",
                     "contacts": "contacts"}


@cache
def _context():
    """Created once per process (on Lambda: per 'warm' container), not per request."""
    store, sessions = create_persistence()
    return create_llm(), store, sessions, load_customers()


def _json(status: int, data: dict) -> tuple[int, dict, bytes]:
    return status, {"content-type": "application/json; charset=utf-8"}, \
        json.dumps(data, ensure_ascii=False, default=str).encode()


def _static(name: str) -> tuple[int, dict, bytes]:
    path = (config.WEB_DIR / name).resolve()
    if not path.is_relative_to(config.WEB_DIR.resolve()) or not path.is_file():  # no path traversal
        return _json(404, {"error": "not_found"})
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return 200, {"content-type": f"{kind}; charset=utf-8" if kind.startswith("text/") or "javascript" in kind
                 else kind}, path.read_bytes()


def handle(method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    if method == "GET" and path in PAGES:
        return _static(PAGES[path])
    if method == "GET" and path.startswith("/static/"):
        return _static(path.removeprefix("/"))
    if not path.startswith("/api/"):
        return _json(404, {"error": "not_found"})

    # Demo token: not production auth (that would be Cognito/IAM), it only prevents
    # anyone with the URL from spending model invocations.
    token = os.getenv("AGIL_DEMO_TOKEN")
    if token and not hmac.compare_digest(headers.get("x-demo-token", ""), token):
        return _json(401, {"error": "unauthorized"})
    try:
        data = json.loads(body or b"{}") if method == "POST" else {}
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return _json(400, {"error": "invalid_json"})
    route = ROUTES.get((method, path))
    if route is None:
        return _json(404, {"error": "not_found"})
    try:
        return route(data, query)
    except Exception:  # never leak a stack trace to the client; do keep it in the logs
        traceback.print_exc()
        return _json(500, {"error": "internal"})


# -- /api routes --------------------------------------------------------------
def info(_d, _q):
    llm = _context()[0]
    return _json(200, {"mode": config.provider(), "label": llm.label,
                       "today": str(config.today()), "policy": config.POLICY})


def customers(_d, _q):
    _, store, _, everyone = _context()
    return _json(200, {"customers": [
        {"customer_id": c["customer_id"], "name": c["name"], "type": c["type"], "segment": segment_of(c),
         "overdue_balance": balance_of(c),
         "days_past_due": c["days_past_due"], "national_id_hint": c["national_id"],
         "scenario": SCENARIOS.get(cid, ""),
         "opt_in": bool(c.get("opt_in")), "last_contact": campaigns.last_contact(c, store)}
        for cid, c in everyone.items()]})


def message(d, _q):
    llm, store, sessions, everyone = _context()
    cid, text = d.get("customer_id"), d.get("text")
    if cid not in everyone:
        return _json(400, {"error": "unknown customer_id"})
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return _json(400, {"error": f"text empty or longer than {MAX_TEXT} characters"})

    start, events = time.monotonic(), []
    agent = Agent(llm, store, everyone, on_event=events.append)  # events of THIS turn only
    if saved := sessions.load(cid):  # the Lambda remembers nothing between invocations
        agent.sessions[cid] = Session.from_dict(saved)
    turn = agent.respond(cid, text.strip())
    session = agent.sessions[cid]
    sessions.save(cid, session.to_dict())
    case = {k: v for k, v in session.case.items() if k != "ok"} if session.case else None
    return _json(200, {"reply": turn.reply, "decision": turn.decision, "events": events,
                       "case": case, "latency_ms": round((time.monotonic() - start) * 1000),
                       "mode": config.provider()})


def reset(d, _q):
    _, store, sessions, _ = _context()
    if cid := d.get("customer_id"):
        sessions.delete(cid)
        store.delete_for("conversation", cid)
    else:
        sessions.delete()
        store.reset()
    return _json(200, {"ok": True})


def operations(_d, _q):
    _, store, _, everyone = _context()
    out = {}
    for key, table in OPERATIONS_TABLES.items():
        rows = [{**i, "name": everyone.get(i["customer_id"], {}).get("name", "")} for i in store.all(table)]
        out[key] = sorted(rows, key=lambda i: i.get("created", ""), reverse=True)
    out["campaigns"] = [{k: c[k] for k in ("campaign_id", "executed_at", "summary")}
                        for c in sorted(store.all("campaigns"), key=lambda c: c["created"], reverse=True)]
    return _json(200, out)


def audit_log(_d, q):
    try:
        limit = max(1, min(int(q.get("limit", 100)), 500))
    except ValueError:
        return _json(400, {"error": "invalid limit"})
    return _json(200, {"events": read_audit(q.get("customer_id") or None, limit)})


def campaign_segments(_d, _q):
    return _json(200, {"segments": campaigns.list_segments(_context()[3]), "template": campaigns.TEMPLATE})


def campaign_run(d, _q):
    _, store, _, everyone = _context()
    requested = d.get("segments") or []
    if not isinstance(requested, list) or set(requested) - set(campaigns.SEGMENT_NAMES):
        return _json(400, {"error": f"valid segments: {', '.join(campaigns.SEGMENT_NAMES)}"})
    return _json(200, campaigns.run_campaign(everyone, store, requested, force_hours=d.get("force_hours") is True))


def conversation(_d, q):
    _, store, _, everyone = _context()
    if (cid := q.get("customer_id")) not in everyone:
        return _json(400, {"error": "unknown customer_id"})
    return _json(200, {"messages": [{**{k: v for k, v in m.items() if k not in ("customer_id", "created")},
                                     "ts": m["created"]} for m in store.find("conversation", cid)]})


ROUTES = {("GET", "/api/info"): info, ("GET", "/api/customers"): customers, ("POST", "/api/message"): message,
          ("POST", "/api/reset"): reset, ("GET", "/api/operations"): operations,
          ("GET", "/api/audit"): audit_log, ("GET", "/api/campaigns/segments"): campaign_segments,
          ("POST", "/api/campaigns/run"): campaign_run, ("GET", "/api/conversation"): conversation}


# -- adapters -------------------------------------------------------------------
def lambda_handler(event, context):
    """API Gateway HTTP API, payload v2 (route $default -> this function). It also
    receives the daily EventBridge Scheduler event: same Lambda, another trigger."""
    if event.get("action") == "daily_campaign":
        _, store, _, everyone = _context()
        return campaigns.run_campaign(everyone, store)["summary"]
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    status, headers, out = handle(event["requestContext"]["http"]["method"], event.get("rawPath", "/"),
                                  event.get("queryStringParameters") or {},
                                  {k.lower(): v for k, v in (event.get("headers") or {}).items()}, body)
    # Always base64: a single path for HTML, JSON or binaries.
    return {"statusCode": status, "headers": headers, "isBase64Encoded": True,
            "body": base64.b64encode(out).decode()}


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Single-threaded local server: in-memory sessions and the JSON store are not
    thread-safe, and for a demo serving one request at a time is enough."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qsl, urlsplit

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            status, headers, out = handle(self.command, url.path, dict(parse_qsl(url.query)),
                                          {k.lower(): v for k, v in self.headers.items()},
                                          self.rfile.read(length))
            self.send_response(status)
            for k, v in {**headers, "content-length": str(len(out))}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _handle

    print(f"Ágil serving at http://{host}:{port}  (backend={config.BACKEND}, mode={config.provider()})")
    HTTPServer((host, port), Handler).serve_forever()
