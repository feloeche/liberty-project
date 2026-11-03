"""API HTTP con backend local + mock: el mismo handle() que corre en Lambda."""
import base64
import json

import pytest

from agil import api
from agil.agent import Sesion


@pytest.fixture(autouse=True)
def limpio(monkeypatch):
    api._contexto.cache_clear()  # store y sesiones nuevos por test (DIR_VAR ya es temporal)
    monkeypatch.delenv("AGIL_DEMO_TOKEN", raising=False)


def llamar(method, path, body=None, headers=None, query=None):
    status, _, salida = api.handle(method, path, query or {}, headers or {},
                                   json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(salida)


def test_info_y_clientes():
    status, info = llamar("GET", "/api/info")
    assert status == 200 and info["modo"] == "mock" and info["hoy"] == "2026-11-02"
    _, datos = llamar("GET", "/api/clientes")
    ana = datos["clientes"][0]
    assert ana["cliente_id"] == "C001" and ana["segmento"] == "olvidadizo" and ana["saldo_vencido"] == 38.5


def test_mensaje_de_punta_a_punta_con_sesion_entre_turnos():
    """Escenario 1 de la demo por HTTP: la sesión (verificación) sobrevive entre requests."""
    _, t1 = llamar("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Mi cédula termina en 4521"})
    assert t1["decision"] == "GESTIONA" and t1["eventos"][0]["tipo"] == "verificacion"
    _, t2 = llamar("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Mándame el link para pagar"})
    assert [e["nombre"] for e in t2["eventos"]] == ["generar_link_pago"]  # solo eventos de ESTE turno
    _, op = llamar("GET", "/api/operacion")
    assert op["links"][0]["nombre"] == "Ana Rodríguez"
    _, aud = llamar("GET", "/api/auditoria", query={"cliente_id": "C001", "limite": "1"})
    assert aud["eventos"][0]["evento"] == "mensaje_saliente"


def test_escalamiento_devuelve_caso():
    _, t = llamar("POST", "/api/mensaje", {"cliente_id": "C004", "texto": "No reconozco este cobro"})
    assert t["decision"] == "ESCALA" and t["caso"]["cola"] == "reclamos"


def test_reiniciar_borra_la_sesion():
    llamar("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Cédula 4521"})
    llamar("POST", "/api/reiniciar", {"cliente_id": "C001"})
    _, t = llamar("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "¿cuánto debo?"})
    assert "cédula" in t["respuesta"]  # volvió a pedir verificación


@pytest.mark.parametrize("body", [{"cliente_id": "C999", "texto": "hola"}, {"cliente_id": "C001", "texto": " "},
                                  {"cliente_id": "C001", "texto": "x" * 1001}])
def test_validaciones_400(body):
    assert llamar("POST", "/api/mensaje", body)[0] == 400


def test_token_401(monkeypatch):
    monkeypatch.setenv("AGIL_DEMO_TOKEN", "secreto-demo")
    assert llamar("GET", "/api/info") == (401, {"error": "no_autorizado"})
    assert llamar("GET", "/api/info", headers={"x-demo-token": "secreto-demo"})[0] == 200
    assert api.handle("GET", "/", {}, {}, b"")[0] == 200  # la web estática no exige token


def test_lambda_handler_payload_v2():
    evento = {"rawPath": "/api/mensaje", "requestContext": {"http": {"method": "POST"}},
              "headers": {"Content-Type": "application/json"}, "isBase64Encoded": True,
              "body": base64.b64encode(json.dumps({"cliente_id": "C001", "texto": "hola"}).encode()).decode()}
    r = api.lambda_handler(evento, None)
    assert r["statusCode"] == 200 and json.loads(base64.b64decode(r["body"]))["decision"] == "GESTIONA"


def test_estaticos_sin_path_traversal():
    assert api.handle("GET", "/static/../agil/api.py", {}, {}, b"")[0] == 404


def test_sesion_ida_y_vuelta_json():
    s = Sesion("C001", verificado=True, insultos=1, montos_validos={38.5, 10.0},
               mensajes=[{"role": "user", "content": [{"type": "text", "text": "hola"}]}])
    copia = Sesion.de_dict(json.loads(json.dumps(s.a_dict())))
    assert copia == s
