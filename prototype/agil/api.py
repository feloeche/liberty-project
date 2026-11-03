"""API HTTP de la demo (web estática + /api/*), sin frameworks.

El núcleo es `handle()`, una función pura (método, ruta, query, headers, body)
-> (status, headers, body). Encima van dos adaptadores delgados:
  - lambda_handler: API Gateway HTTP API (payload v2) -> handle()
  - servir:         http.server de la stdlib para `python -m agil serve`
Así local y AWS ejecutan exactamente el mismo código, y no se empaqueta
FastAPI/Flask en la Lambda (menos dependencias, arranque en frío más corto)."""
import base64
import hmac
import json
import mimetypes
import os
import time
import traceback
from functools import cache

from . import campanas, config
from .agent import Agente, Sesion
from .llm import crear_llm
from .store import cargar_clientes, crear_persistencia, leer_auditoria
from .tools import saldo_de, segmentar

MAX_TEXTO = 1000
PAGINAS = {"/": "index.html", "/operacion": "operacion.html"}
# Etiqueta del escenario sugerido por cliente (los mismos de la demo de la CLI).
ESCENARIOS = {"C001": "Olvidadizo: link de pago", "C002": "Dificultad temporal: promesa ajustada",
              "C003": "Reincidente: pide 6 cuotas y escala", "C004": "En disputa: escala sin cobrar",
              "C005": "B2B: escala a un ejecutivo", "C006": "Acuerdo en 3 cuotas",
              "C007": "Vulnerable: escala con prioridad"}
TABLAS_OPERACION = {"casos": "escalamientos", "promesas": "promesas", "acuerdos": "acuerdos", "links": "links",
                    "contactos": "contactos"}


@cache
def _contexto():
    """Se crea una vez por proceso (en Lambda: por contenedor 'tibio'), no por request."""
    store, sesiones = crear_persistencia()
    return crear_llm(), store, sesiones, cargar_clientes()


def _json(status: int, datos: dict) -> tuple[int, dict, bytes]:
    return status, {"content-type": "application/json; charset=utf-8"}, \
        json.dumps(datos, ensure_ascii=False, default=str).encode()


def _estatico(nombre: str) -> tuple[int, dict, bytes]:
    ruta = (config.DIR_WEB / nombre).resolve()
    if not ruta.is_relative_to(config.DIR_WEB.resolve()) or not ruta.is_file():  # sin path traversal
        return _json(404, {"error": "no_encontrado"})
    tipo = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
    return 200, {"content-type": f"{tipo}; charset=utf-8" if tipo.startswith("text/") or "javascript" in tipo
                 else tipo}, ruta.read_bytes()


def handle(method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    if method == "GET" and path in PAGINAS:
        return _estatico(PAGINAS[path])
    if method == "GET" and path.startswith("/static/"):
        return _estatico(path.removeprefix("/"))
    if not path.startswith("/api/"):
        return _json(404, {"error": "no_encontrado"})

    # Token de demo: no es auth de producción (eso sería Cognito/IAM), solo evita
    # que cualquiera con la URL gaste invocaciones del modelo.
    token = os.getenv("AGIL_DEMO_TOKEN")
    if token and not hmac.compare_digest(headers.get("x-demo-token", ""), token):
        return _json(401, {"error": "no_autorizado"})
    try:
        datos = json.loads(body or b"{}") if method == "POST" else {}
        if not isinstance(datos, dict):
            raise ValueError
    except ValueError:
        return _json(400, {"error": "json_invalido"})
    ruta = RUTAS.get((method, path))
    if ruta is None:
        return _json(404, {"error": "no_encontrado"})
    try:
        return ruta(datos, query)
    except Exception:  # nunca filtrar un stacktrace al cliente; sí dejarlo en los logs
        traceback.print_exc()
        return _json(500, {"error": "interno"})


# -- rutas /api -------------------------------------------------------------
def info(_d, _q):
    llm = _contexto()[0]
    return _json(200, {"modo": config.proveedor(), "etiqueta": llm.etiqueta,
                       "hoy": str(config.hoy()), "politica": config.POLITICA})


def clientes(_d, _q):
    _, store, _, todos = _contexto()
    return _json(200, {"clientes": [
        {"cliente_id": c["cliente_id"], "nombre": c["nombre"], "tipo": c["tipo"], "segmento": segmentar(c),
         "saldo_vencido": saldo_de(c),
         "dias_mora": c["dias_mora"], "pista_cedula": c["cedula"], "escenario": ESCENARIOS.get(cid, ""),
         "opt_in": bool(c.get("opt_in")), "ultimo_contacto": campanas.ultimo_contacto(c, store)}
        for cid, c in todos.items()]})


def mensaje(d, _q):
    llm, store, sesiones, todos = _contexto()
    cid, texto = d.get("cliente_id"), d.get("texto")
    if cid not in todos:
        return _json(400, {"error": "cliente_id desconocido"})
    if not isinstance(texto, str) or not texto.strip() or len(texto) > MAX_TEXTO:
        return _json(400, {"error": f"texto vacío o de más de {MAX_TEXTO} caracteres"})

    inicio, eventos = time.monotonic(), []
    agente = Agente(llm, store, todos, al_evento=eventos.append)  # eventos solo de ESTE turno
    if guardada := sesiones.cargar(cid):  # la Lambda no recuerda nada entre invocaciones
        agente.sesiones[cid] = Sesion.de_dict(guardada)
    turno = agente.responder(cid, texto.strip())
    sesion = agente.sesiones[cid]
    sesiones.guardar(cid, sesion.a_dict())
    caso = {k: v for k, v in sesion.caso.items() if k != "ok"} if sesion.caso else None
    return _json(200, {"respuesta": turno.respuesta, "decision": turno.decision, "eventos": eventos,
                       "caso": caso, "latencia_ms": round((time.monotonic() - inicio) * 1000),
                       "modo": config.proveedor()})


def reiniciar(d, _q):
    _, store, sesiones, _ = _contexto()
    if cid := d.get("cliente_id"):
        sesiones.borrar(cid)
        store.borrar_de("conversacion", cid)
    else:
        sesiones.borrar()
        store.reiniciar()
    return _json(200, {"ok": True})


def operacion(_d, _q):
    _, store, _, todos = _contexto()
    salida = {}
    for clave, tabla in TABLAS_OPERACION.items():
        filas = [{**i, "nombre": todos.get(i["cliente_id"], {}).get("nombre", "")} for i in store.todos(tabla)]
        salida[clave] = sorted(filas, key=lambda i: i.get("creado", ""), reverse=True)
    salida["campanas"] = [{k: c[k] for k in ("campana_id", "ejecutada", "resumen")}
                          for c in sorted(store.todos("campanas"), key=lambda c: c["creado"], reverse=True)]
    return _json(200, salida)


def auditoria(_d, q):
    try:
        limite = max(1, min(int(q.get("limite", 100)), 500))
    except ValueError:
        return _json(400, {"error": "limite inválido"})
    return _json(200, {"eventos": leer_auditoria(q.get("cliente_id") or None, limite)})


def campana_segmentos(_d, _q):
    return _json(200, {"segmentos": campanas.listar_segmentos(_contexto()[3]), "plantilla": campanas.PLANTILLA})


def campana_ejecutar(d, _q):
    _, store, _, todos = _contexto()
    pedidos = d.get("segmentos") or []
    if not isinstance(pedidos, list) or set(pedidos) - set(campanas.NOMBRES):
        return _json(400, {"error": f"segmentos válidos: {', '.join(campanas.NOMBRES)}"})
    return _json(200, campanas.ejecutar_campana(todos, store, pedidos, forzar_horario=d.get("forzar_horario") is True))


def conversacion(_d, q):
    _, store, _, todos = _contexto()
    if (cid := q.get("cliente_id")) not in todos:
        return _json(400, {"error": "cliente_id desconocido"})
    return _json(200, {"mensajes": [{**{k: v for k, v in m.items() if k not in ("cliente_id", "creado")},
                                     "ts": m["creado"]} for m in store.buscar("conversacion", cid)]})


RUTAS = {("GET", "/api/info"): info, ("GET", "/api/clientes"): clientes, ("POST", "/api/mensaje"): mensaje,
         ("POST", "/api/reiniciar"): reiniciar, ("GET", "/api/operacion"): operacion,
         ("GET", "/api/auditoria"): auditoria, ("GET", "/api/campanas/segmentos"): campana_segmentos,
         ("POST", "/api/campanas/ejecutar"): campana_ejecutar, ("GET", "/api/conversacion"): conversacion}


# -- adaptadores ------------------------------------------------------------
def lambda_handler(event, context):
    """API Gateway HTTP API, payload v2 (ruta $default -> esta función). También
    recibe el evento diario de EventBridge Scheduler: misma Lambda, otro disparador."""
    if event.get("accion") == "campana_diaria":
        _, store, _, todos = _contexto()
        return campanas.ejecutar_campana(todos, store)["resumen"]
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    status, headers, salida = handle(event["requestContext"]["http"]["method"], event.get("rawPath", "/"),
                                     event.get("queryStringParameters") or {},
                                     {k.lower(): v for k, v in (event.get("headers") or {}).items()}, body)
    # Siempre base64: un solo camino para HTML, JSON o binarios.
    return {"statusCode": status, "headers": headers, "isBase64Encoded": True,
            "body": base64.b64encode(salida).decode()}


def servir(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Servidor local de un solo hilo: las sesiones en memoria y el store JSON no
    son thread-safe, y para una demo atender un request a la vez es suficiente."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qsl, urlsplit

    class Manejador(BaseHTTPRequestHandler):
        def _atender(self):
            url = urlsplit(self.path)
            largo = int(self.headers.get("content-length") or 0)
            status, headers, salida = handle(self.command, url.path, dict(parse_qsl(url.query)),
                                             {k.lower(): v for k, v in self.headers.items()},
                                             self.rfile.read(largo))
            self.send_response(status)
            for k, v in {**headers, "content-length": str(len(salida))}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(salida)

        do_GET = do_POST = _atender

    print(f"Ágil sirviendo en http://{host}:{port}  (backend={config.BACKEND}, modo={config.proveedor()})")
    HTTPServer((host, port), Manejador).serve_forever()
