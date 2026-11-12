"""AWS backend (AGIL_BACKEND=aws): ONE DynamoDB table for all the state.

Single-table design (pk = customer_id), because every agent access is
"this customer's data", so each turn is resolved with a Query per partition:
  sk = "<table>#<ts>#<id>"  -> promises, plans, links, escalations (Store)
  sk = "SESSION"            -> conversation memory, with a 24 h TTL
                               (the WhatsApp service window)
  sk = "AUDIT#<ts>#<id>"    -> audit trail read by /api/audit
The content is stored as JSON in the `data` attribute: DynamoDB requires Decimal
instead of float, and serializing avoids converting amounts on every read and write.
Demo simplifications: the global views (operations, case count) use a bounded
Scan; in production they would be a GSI and an atomic counter, and the session
would carry optimistic locking (version) against concurrent turns."""
import json
import time
import uuid
from functools import cache

from . import config
from .store import now

SESSION_TTL_S = 24 * 3600
MAX_SCAN_PAGES = 5  # cost cap: the demo table is small


@cache
def table():
    import boto3  # included in the Lambda runtime
    return boto3.resource("dynamodb").Table(config.DYNAMO_TABLE)


def _sk(prefix: str) -> str:
    return f"{prefix}#{now()}#{uuid.uuid4().hex[:8]}"


def _query(customer_id: str, prefix: str, **kw) -> list[dict]:
    from boto3.dynamodb.conditions import Key
    r = table().query(KeyConditionExpression=Key("pk").eq(customer_id) & Key("sk").begins_with(prefix), **kw)
    return [json.loads(i["data"]) for i in r["Items"]]


def _scan(**kw):
    """Paginated, bounded Scan (generator of raw items)."""
    for _ in range(MAX_SCAN_PAGES):
        r = table().scan(**kw)
        yield from r["Items"]
        if "LastEvaluatedKey" not in r:
            return
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


class DynamoStore:
    """Same interface as store.Store, on top of DynamoDB."""

    def save(self, t: str, item: dict) -> dict:
        item = {**item, "created": now()}
        table().put_item(Item={"pk": item["customer_id"], "sk": _sk(t), "table": t,
                               "data": json.dumps(item, ensure_ascii=False)})
        return item

    def find(self, t: str, customer_id: str) -> list[dict]:
        return _query(customer_id, f"{t}#")

    def count(self, t: str) -> int:
        from boto3.dynamodb.conditions import Attr
        return sum(1 for _ in _scan(FilterExpression=Attr("table").eq(t), ProjectionExpression="pk"))

    def all(self, t: str) -> list[dict]:
        from boto3.dynamodb.conditions import Attr
        return [json.loads(i["data"]) for i in _scan(FilterExpression=Attr("table").eq(t))]

    def delete_for(self, t: str, customer_id: str) -> None:
        from boto3.dynamodb.conditions import Key
        r = table().query(KeyConditionExpression=Key("pk").eq(customer_id) & Key("sk").begins_with(f"{t}#"),
                          ProjectionExpression="pk, sk")
        with table().batch_writer() as batch:
            for i in r["Items"]:
                batch.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})

    def reset(self) -> None:
        """Deletes business state and sessions; the audit trail is kept."""
        from boto3.dynamodb.conditions import Attr
        with table().batch_writer() as batch:
            for i in _scan(FilterExpression=~Attr("sk").begins_with("AUDIT#"), ProjectionExpression="pk, sk"):
                batch.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})


class DynamoSessions:
    """The Lambda is stateless: the session is loaded at the start of the turn and saved at the end."""

    def load(self, customer_id: str) -> dict | None:
        item = table().get_item(Key={"pk": customer_id, "sk": "SESSION"}).get("Item")
        # DynamoDB TTL deletes with a delay (up to hours): it's also checked on read.
        return json.loads(item["data"]) if item and item["ttl"] > time.time() else None

    def save(self, customer_id: str, session: dict) -> None:
        table().put_item(Item={"pk": customer_id, "sk": "SESSION", "ttl": int(time.time()) + SESSION_TTL_S,
                               "data": json.dumps(session, ensure_ascii=False, default=str)})

    def delete(self, customer_id: str | None = None) -> None:
        if customer_id:
            table().delete_item(Key={"pk": customer_id, "sk": "SESSION"})
        else:
            DynamoStore().reset()  # includes everyone's sessions


def audit(line: dict) -> None:
    """Two destinations: Lambda stdout = CloudWatch Logs (one JSON per line,
    queryable with Logs Insights) and the table, so the web can show the trace."""
    text = json.dumps(line, ensure_ascii=False, default=str)
    print(text, flush=True)
    table().put_item(Item={"pk": line["customer_id"], "sk": _sk("AUDIT"), "data": text})


def read_audit(customer_id: str | None, limit: int) -> list[dict]:
    if customer_id:
        return _query(customer_id, "AUDIT#", ScanIndexForward=False, Limit=limit)
    from boto3.dynamodb.conditions import Attr
    events = [json.loads(i["data"]) for i in _scan(FilterExpression=Attr("sk").begins_with("AUDIT#"))]
    return sorted(events, key=lambda e: e["ts"], reverse=True)[:limit]
