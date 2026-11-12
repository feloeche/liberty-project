"""AWS backend against DynamoDB simulated with moto.

Why moto and not a hand-written fake: the code uses real boto3 expressions
(begins_with, filters, pagination, batch_writer); a fake would have to
reimplement them and could pass even if the real call failed. moto is only a
dev dependency: it is not packaged into the Lambda."""
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
            TableName=config.DYNAMO_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        dynamo.table.cache_clear()
        api._context.cache_clear()
        yield
        dynamo.table.cache_clear()
        api._context.cache_clear()


def post(path, body):
    status, _, out = api.handle("POST", path, {}, {}, json.dumps(body).encode())
    return status, json.loads(out)


def test_dynamo_store_same_interface():
    s = dynamo.DynamoStore()
    s.save("promises", {"customer_id": "C001", "amount": 10.5, "status": "active"})
    s.save("promises", {"customer_id": "C002", "amount": 20.0, "status": "active"})
    assert [p["amount"] for p in s.find("promises", "C001")] == [10.5]  # float, no Decimal
    assert s.count("promises") == 2 and len(s.all("promises")) == 2
    s.reset()
    assert s.count("promises") == 0


def test_expired_session_is_not_loaded():
    sessions = dynamo.DynamoSessions()
    sessions.save("C001", {"customer_id": "C001"})
    assert sessions.load("C001") == {"customer_id": "C001"}
    dynamo.table().update_item(Key={"pk": "C001", "sk": "SESSION"}, UpdateExpression="SET #t = :v",
                               ExpressionAttributeNames={"#t": "ttl"}, ExpressionAttributeValues={":v": int(time.time()) - 1})
    assert sessions.load("C001") is None


def test_stateless_conversation_over_http(capsys):
    """Two requests = two 'invocations': the session lives in DynamoDB, not in memory."""
    post("/api/message", {"customer_id": "C001", "text": "Mi cédula termina en 4521"})
    api._context.cache_clear()  # simulates a new Lambda container
    _, t = post("/api/message", {"customer_id": "C001", "text": "Mándame el link para pagar"})
    assert [e["name"] for e in t["events"]] == ["create_payment_link"]
    assert '"event": "outbound_message"' in capsys.readouterr().out  # audit to CloudWatch (stdout)
    _, _, out = api.handle("GET", "/api/audit", {"customer_id": "C001", "limit": "1"}, {}, b"")
    assert json.loads(out)["events"][0]["event"] == "outbound_message"
    post("/api/reset", {})
    _, _, out = api.handle("GET", "/api/operations", {}, {}, b"")
    assert json.loads(out)["links"] == []


def test_campaign_and_conversation_on_dynamo(monkeypatch):
    """Contacts, campaigns and conversation share the table (pk = customer_id; campaigns, pk = SYSTEM)."""
    from datetime import datetime
    from agil import campaigns
    monkeypatch.setattr(campaigns, "panama_now", lambda: datetime(2026, 11, 2, 10, tzinfo=campaigns.PANAMA))
    _, r = post("/api/campaigns/run", {"segments": ["forgetful"]})
    assert r["summary"]["sent"] == 1
    _, t = post("/api/message", {"customer_id": "C001", "text": "Hola"})
    assert t["events"][0]["type"] == "campaign"
    _, _, out = api.handle("GET", "/api/conversation", {"customer_id": "C001"}, {}, b"")
    assert [m["role"] for m in json.loads(out)["messages"]] == ["campaign", "customer", "agent"]
    _, r2 = post("/api/campaigns/run", {"segments": ["forgetful"]})
    assert r2["campaign_id"].endswith("-0002") and r2["results"][0]["reason"] == "recently_contacted"


def test_deleting_a_customer_conversation_keeps_promises():
    s = dynamo.DynamoStore()
    s.save("conversation", {"customer_id": "C001", "role": "customer", "text": "hola"})
    s.save("promises", {"customer_id": "C001", "amount": 10.0, "status": "active"})
    s.delete_for("conversation", "C001")
    assert s.find("conversation", "C001") == [] and len(s.find("promises", "C001")) == 1
