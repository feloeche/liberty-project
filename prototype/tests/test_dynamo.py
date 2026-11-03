"""Backend AWS contra DynamoDB simulado con moto.

Por qué moto y no un fake propio: el código usa expresiones reales de boto3
(begins_with, filtros, paginación, batch_writer); un fake tendría que
reimplementarlas y podría pasar aunque la llamada real fallara. moto es solo
dependencia de desarrollo: no entra al paquete de la Lambda."""
import json
import time

import pytest

moto = pytest.importorskip("moto")

from agil import api, config, dynamo  # noqa: E402


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AGIL_DEMO_TOKEN", raising=False)
    monkeypatch.setattr(config, "BACKEND", "aws")
    with moto.mock_aws():
        import boto3
        boto3.client("dynamodb").create_table(
            TableName=config.TABLA_DYNAMO, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        dynamo.tabla.cache_clear()
        api._contexto.cache_clear()
        yield
        dynamo.tabla.cache_clear()
        api._contexto.cache_clear()


def post(path, body):
    status, _, salida = api.handle("POST", path, {}, {}, json.dumps(body).encode())
    return status, json.loads(salida)


def test_store_dynamo_misma_interfaz():
    s = dynamo.DynamoStore()
    s.guardar("promesas", {"cliente_id": "C001", "monto": 10.5, "estado": "activa"})
    s.guardar("promesas", {"cliente_id": "C002", "monto": 20.0, "estado": "activa"})
    assert [p["monto"] for p in s.buscar("promesas", "C001")] == [10.5]  # float sin Decimal
    assert s.contar("promesas") == 2 and len(s.todos("promesas")) == 2
    s.reiniciar()
    assert s.contar("promesas") == 0


def test_sesion_expirada_no_se_carga():
    ses = dynamo.SesionesDynamo()
    ses.guardar("C001", {"cliente_id": "C001"})
    assert ses.cargar("C001") == {"cliente_id": "C001"}
    dynamo.tabla().update_item(Key={"pk": "C001", "sk": "SESION"}, UpdateExpression="SET #t = :v",
                               ExpressionAttributeNames={"#t": "ttl"}, ExpressionAttributeValues={":v": int(time.time()) - 1})
    assert ses.cargar("C001") is None


def test_conversacion_stateless_por_http(capsys):
    """Dos requests = dos 'invocaciones': la sesión vive en DynamoDB, no en memoria."""
    post("/api/mensaje", {"cliente_id": "C001", "texto": "Mi cédula termina en 4521"})
    api._contexto.cache_clear()  # simula un contenedor Lambda nuevo
    _, t = post("/api/mensaje", {"cliente_id": "C001", "texto": "Mándame el link para pagar"})
    assert [e["nombre"] for e in t["eventos"]] == ["generar_link_pago"]
    assert '"evento": "mensaje_saliente"' in capsys.readouterr().out  # auditoría a CloudWatch (stdout)
    _, _, salida = api.handle("GET", "/api/auditoria", {"cliente_id": "C001", "limite": "1"}, {}, b"")
    assert json.loads(salida)["eventos"][0]["evento"] == "mensaje_saliente"
    post("/api/reiniciar", {})
    _, _, salida = api.handle("GET", "/api/operacion", {}, {}, b"")
    assert json.loads(salida)["links"] == []


def test_campana_y_conversacion_en_dynamo(monkeypatch):
    """Contactos, campañas y conversación usan la misma tabla (pk = cliente_id; la campaña, pk = SISTEMA)."""
    from datetime import datetime
    from agil import campanas
    monkeypatch.setattr(campanas, "ahora_panama", lambda: datetime(2026, 11, 2, 10, tzinfo=campanas.PANAMA))
    _, r = post("/api/campanas/ejecutar", {"segmentos": ["olvidadizo"]})
    assert r["resumen"]["enviados"] == 1
    _, t = post("/api/mensaje", {"cliente_id": "C001", "texto": "Hola"})
    assert t["eventos"][0]["tipo"] == "campana"
    _, _, salida = api.handle("GET", "/api/conversacion", {"cliente_id": "C001"}, {}, b"")
    assert [m["rol"] for m in json.loads(salida)["mensajes"]] == ["campana", "cliente", "agente"]
    _, r2 = post("/api/campanas/ejecutar", {"segmentos": ["olvidadizo"]})
    assert r2["campana_id"].endswith("-0002") and r2["resultados"][0]["motivo"] == "contactado_recientemente"


def test_borrar_conversacion_de_un_cliente_no_toca_promesas():
    s = dynamo.DynamoStore()
    s.guardar("conversacion", {"cliente_id": "C001", "rol": "cliente", "texto": "hola"})
    s.guardar("promesas", {"cliente_id": "C001", "monto": 10.0, "estado": "activa"})
    s.borrar_de("conversacion", "C001")
    assert s.buscar("conversacion", "C001") == [] and len(s.buscar("promesas", "C001")) == 1
