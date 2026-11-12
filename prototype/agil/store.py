"""Local persistence that simulates the AWS state services.

- Store  -> DynamoDB (promises, payment plans, links, escalated cases).
- audit  -> CloudWatch Logs / transcripts in S3 (one JSON event per line).
Plain JSON was chosen over SQLite because it can be opened and read during the demo.
With AGIL_BACKEND=aws the same interfaces are implemented on top of DynamoDB
(see dynamo.py); the agent and the tools don't notice the difference."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config


def now() -> str:
    # Microseconds: the timestamp is part of the DynamoDB sort key and orders the items.
    # UTC with an explicit offset (+00:00): without it, the browser read the server time
    # (UTC on Lambda) as local time and the template showed up at 01:01 a.m.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    """One 'table' per entity; every item carries customer_id (the DynamoDB partition key)."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.VAR_DIR / "store.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, table: str, item: dict) -> dict:
        item = {**item, "created": now()}  # the operations console sorts by date
        self.data.setdefault(table, []).append(item)
        self._write()
        return item

    def find(self, table: str, customer_id: str) -> list[dict]:
        return [i for i in self.data.get(table, []) if i["customer_id"] == customer_id]

    def count(self, table: str) -> int:
        return len(self.data.get(table, []))

    def all(self, table: str) -> list[dict]:
        """Operations view (every row of a table, across all customers)."""
        return list(self.data.get(table, []))

    def delete_for(self, table: str, customer_id: str) -> None:
        """'Reset conversation' for one customer: clears their visible chat, not their promises."""
        self.data[table] = [i for i in self.data.get(table, []) if i["customer_id"] != customer_id]
        self._write()

    def reset(self) -> None:
        self.data = {}
        if self.path.exists():
            self.path.unlink()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


class LocalSessions:
    """In-process memory sessions: enough for the CLI and `serve` (one process).
    Not valid on Lambda, because each invocation may land on a different container."""

    def __init__(self):
        self.data: dict[str, dict] = {}

    def load(self, customer_id: str) -> dict | None:
        return self.data.get(customer_id)

    def save(self, customer_id: str, session: dict) -> None:
        self.data[customer_id] = session

    def delete(self, customer_id: str | None = None) -> None:
        self.data.pop(customer_id, None) if customer_id else self.data.clear()


def create_persistence():
    """(store, sessions) according to AGIL_BACKEND. boto3 is only imported in aws mode."""
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.DynamoStore(), dynamo.DynamoSessions()
    return Store(), LocalSessions()


def audit(event: str, customer_id: str, **data) -> None:
    """Immutable record of EVERYTHING that comes in, goes out, and every tool executed.
    It is the basis for answering 'how do you know the agent said the right thing?'."""
    line = {"ts": now(), "event": event, "customer_id": customer_id, **data}
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.audit(line)
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.VAR_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def read_audit(customer_id: str | None, limit: int) -> list[dict]:
    """Most recent events first (for the web trace panel)."""
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.read_audit(customer_id, limit)
    path = config.VAR_DIR / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    events = [e for e in map(json.loads, reversed(lines)) if not customer_id or e["customer_id"] == customer_id]
    return events[:limit]


def load_customers() -> dict[str, dict]:
    """Mock data that simulates the billing/CRM replica."""
    data = json.loads(config.CUSTOMERS_PATH.read_text(encoding="utf-8"))
    return {c["customer_id"]: c for c in data["customers"]}
