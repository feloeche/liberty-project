"""Proactive contact: suppression rules are deterministic code and are tested one by one."""
import json
import re
from datetime import datetime

import pytest

from agil import api, campaigns
from agil.campaigns import PANAMA, run_campaign, suppression_reason
from agil.store import Store, load_customers

MONDAY_10 = datetime(2026, 11, 2, 10, 0, tzinfo=PANAMA)  # conftest's TODAY is a Monday


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store.json")


@pytest.fixture
def customers():
    return load_customers()


def reason(customer, store, now=MONDAY_10, force=False):
    r = suppression_reason(customer, store, now, force)
    return r and r[0]


def test_campaign_result_with_demo_data(customers, store):
    r = run_campaign(customers, store, now=MONDAY_10)
    statuses = {row["customer_id"]: row.get("reason", row["status"]) for row in r["results"]}
    assert statuses == {"C001": "sent", "C002": "sent", "C003": "recently_contacted",
                        "C004": "open_claim", "C005": "non_collectable_segment", "C006": "no_opt_in",
                        "C007": "sent"}
    assert r["campaign_id"] == "CMP-20261102-0001" and r["summary"]["sent"] == 3
    assert len(store.data["contacts"]) == 3 and store.data["campaigns"][0]["summary"] == r["summary"]


@pytest.mark.parametrize("change, expected", [
    ({"opt_in": False}, "no_opt_in"),
    ({"opt_out": True}, "opt_out"),
    ({"opt_in": False, "opt_out": True}, "no_opt_in"),  # the first rule that applies wins
    ({"overdue_invoices": []}, "no_balance"),
])
def test_reasons_from_customer_data(customers, store, change, expected):
    assert reason({**customers["C001"], **change}, store) == expected


def test_reasons_from_store_state(customers, store):
    store.save("promises", {"customer_id": "C002", "amount": 50, "status": "active"})
    assert reason(customers["C002"], store) == "active_promise"
    store.save("escalations", {"customer_id": "C002", "case_id": "CASE-0001"})
    assert reason(customers["C002"], store) == "open_human_case"  # comes before the promise


@pytest.mark.parametrize("now, allowed", [
    (MONDAY_10, True),
    (datetime(2026, 11, 1, 10, 0, tzinfo=PANAMA), False),  # Sunday
    (datetime(2026, 11, 2, 21, 0, tzinfo=PANAMA), False),  # 21:00
    (datetime(2026, 11, 2, 7, 59, tzinfo=PANAMA), False),
])
def test_contact_hours(customers, store, now, allowed):
    assert (reason(customers["C001"], store, now) != "outside_hours") is allowed
    assert reason(customers["C001"], store, now, force=True) != "outside_hours"


def test_consent_is_checked_before_contact_hours(customers, store):
    sunday = datetime(2026, 11, 1, 10, 0, tzinfo=PANAMA)
    assert reason(customers["C006"], store, sunday) == "no_opt_in"


def test_3_day_rule(customers, store):
    run_campaign(customers, store, ["forgetful"], now=MONDAY_10)
    assert reason(customers["C001"], store) == "recently_contacted"
    thursday = datetime(2026, 11, 5, 10, 0, tzinfo=PANAMA)  # 3 days later: allowed again
    assert reason(customers["C001"], store, thursday) is None


def test_template_does_not_reveal_amounts(customers, store):
    """If the number was reassigned, a third party reads the template: it must not show the debt."""
    r = run_campaign(customers, store, now=MONDAY_10)
    for row in (row for row in r["results"] if row["status"] == "sent"):
        assert not re.search(r"\d|usd|\$|saldo|deuda|mora", row["text"], re.I), row["text"]
        assert row["text"].startswith(f"Hola {customers[row['customer_id']]['name'].split()[0]},")


# -- over HTTP: the same handle() that runs on Lambda -----------------------------
@pytest.fixture
def http(monkeypatch):
    api._context.cache_clear()
    monkeypatch.delenv("AGIL_DEMO_TOKEN", raising=False)
    monkeypatch.setattr(campaigns, "panama_now", lambda: MONDAY_10)

    def call(method, path, body=None, query=None):
        status, _, out = api.handle(method, path, query or {}, {},
                                    json.dumps(body).encode() if body is not None else b"")
        return status, json.loads(out)
    yield call
    api._context.cache_clear()


def test_campaign_and_conversation_endpoints(http):
    _, seg = http("GET", "/api/campaigns/segments")
    assert {s["id"]: s["estimated_eligible"] for s in seg["segments"]}["temporary_hardship"] == 3
    assert seg["template"]["category"] == "UTILITY"
    assert http("POST", "/api/campaigns/run", {"segments": ["vip"]})[0] == 400
    _, r = http("POST", "/api/campaigns/run", {"segments": ["forgetful", "in_dispute"]})
    assert [(row["customer_id"], row["status"]) for row in r["results"]] == [("C001", "sent"), ("C004", "suppressed")]

    # The customer replies: the turn's first event is 'campaign' and the UI sees the full history.
    _, t = http("POST", "/api/message", {"customer_id": "C001", "text": "Hola, ¿qué aviso? Cédula 4521"})
    assert t["events"][0] == {"type": "campaign", "detail": "Customer is replying to template "
                                                            f"account_notice_v1 ({r['campaign_id']})"}
    _, t2 = http("POST", "/api/message", {"customer_id": "C001", "text": "Mándame el link"})
    assert all(e["type"] != "campaign" for e in t2["events"])  # only when replying to the template
    _, conv = http("GET", "/api/conversation", query={"customer_id": "C001"})
    assert [m["role"] for m in conv["messages"]] == ["campaign", "customer", "agent", "customer", "agent"]
    assert conv["messages"][0]["campaign_id"] == r["campaign_id"] and conv["messages"][2]["decision"] == "HANDLED"

    _, ops = http("GET", "/api/operations")
    assert ops["campaigns"][0]["campaign_id"] == r["campaign_id"] and ops["contacts"][0]["name"] == "Ana Rodríguez"
    _, cl = http("GET", "/api/customers")
    by_id = {c["customer_id"]: c for c in cl["customers"]}
    assert by_id["C006"]["opt_in"] is False and by_id["C004"]["last_contact"] is None
    assert by_id["C001"]["last_contact"].startswith("20")


def test_model_sees_the_template_as_its_previous_message(http, monkeypatch):
    """Demo finding: with only a note, Claude greeted again. The template goes in as an
    'assistant' message, preceded by a 'user' notice (the API requires starting with 'user')."""
    http("POST", "/api/campaigns/run", {"segments": ["forgetful"]})
    http("POST", "/api/message", {"customer_id": "C001", "text": "Hola"})
    notice, template, turn = api._context()[2].load("C001")["messages"][:3]
    assert notice["role"] == "user" and "account_notice_v1" in notice["content"][0]["text"]
    assert template["role"] == "assistant" and template["content"][0]["text"].startswith("Hola Ana")
    assert turn["role"] == "user" and "replying_to_template=account_notice_v1" in turn["content"][0]["text"]


def test_scheduler_event_on_lambda(http):
    assert api.lambda_handler({"action": "daily_campaign"}, None)["sent"] == 3
