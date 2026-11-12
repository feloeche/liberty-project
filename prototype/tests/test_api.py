"""HTTP API with local backend + mock: the same handle() that runs on Lambda."""
import base64
import json

import pytest

from agil import api
from agil.agent import Session


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    api._context.cache_clear()  # fresh store and sessions per test (VAR_DIR is already temporary)
    monkeypatch.delenv("AGIL_DEMO_TOKEN", raising=False)


def call(method, path, body=None, headers=None, query=None):
    status, _, out = api.handle(method, path, query or {}, headers or {},
                                json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(out)


def test_info_and_customers():
    status, info = call("GET", "/api/info")
    assert status == 200 and info["mode"] == "mock" and info["today"] == "2026-11-02"
    _, data = call("GET", "/api/customers")
    ana = data["customers"][0]
    assert ana["customer_id"] == "C001" and ana["segment"] == "forgetful" and ana["overdue_balance"] == 38.5


def test_message_end_to_end_with_session_across_turns():
    """Demo scenario 1 over HTTP: the session (verification) survives between requests."""
    _, t1 = call("POST", "/api/message", {"customer_id": "C001", "text": "Mi cédula termina en 4521"})
    assert t1["decision"] == "HANDLED" and t1["events"][0]["type"] == "verification"
    _, t2 = call("POST", "/api/message", {"customer_id": "C001", "text": "Mándame el link para pagar"})
    assert [e["name"] for e in t2["events"]] == ["create_payment_link"]  # only THIS turn's events
    _, ops = call("GET", "/api/operations")
    assert ops["links"][0]["name"] == "Ana Rodríguez"
    _, aud = call("GET", "/api/audit", query={"customer_id": "C001", "limit": "1"})
    assert aud["events"][0]["event"] == "outbound_message"


def test_escalation_returns_case():
    _, t = call("POST", "/api/message", {"customer_id": "C004", "text": "No reconozco este cobro"})
    assert t["decision"] == "ESCALATED" and t["case"]["queue"] == "claims"


def test_reset_clears_the_session():
    call("POST", "/api/message", {"customer_id": "C001", "text": "Cédula 4521"})
    call("POST", "/api/reset", {"customer_id": "C001"})
    _, t = call("POST", "/api/message", {"customer_id": "C001", "text": "¿cuánto debo?"})
    assert "cédula" in t["reply"]  # it asked for verification again


@pytest.mark.parametrize("body", [{"customer_id": "C999", "text": "hola"}, {"customer_id": "C001", "text": " "},
                                  {"customer_id": "C001", "text": "x" * 1001}])
def test_validations_400(body):
    assert call("POST", "/api/message", body)[0] == 400


def test_token_401(monkeypatch):
    monkeypatch.setenv("AGIL_DEMO_TOKEN", "demo-secret")
    assert call("GET", "/api/info") == (401, {"error": "unauthorized"})
    assert call("GET", "/api/info", headers={"x-demo-token": "demo-secret"})[0] == 200
    assert api.handle("GET", "/", {}, {}, b"")[0] == 200  # the static web does not require the token


def test_lambda_handler_payload_v2():
    event = {"rawPath": "/api/message", "requestContext": {"http": {"method": "POST"}},
             "headers": {"Content-Type": "application/json"}, "isBase64Encoded": True,
             "body": base64.b64encode(json.dumps({"customer_id": "C001", "text": "hola"}).encode()).decode()}
    r = api.lambda_handler(event, None)
    assert r["statusCode"] == 200 and json.loads(base64.b64decode(r["body"]))["decision"] == "HANDLED"


def test_static_files_without_path_traversal():
    assert api.handle("GET", "/static/../agil/api.py", {}, {}, b"")[0] == 404
    assert api.handle("GET", "/operations", {}, {}, b"")[0] == 200


def test_session_json_round_trip():
    s = Session("C001", verified=True, insults=1, valid_amounts={38.5, 10.0},
                messages=[{"role": "user", "content": [{"type": "text", "text": "hola"}]}])
    copy = Session.from_dict(json.loads(json.dumps(s.to_dict())))
    assert copy == s
