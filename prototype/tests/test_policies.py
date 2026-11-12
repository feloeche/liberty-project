"""Policies live in the tools' code: this proves the LLM cannot bypass them."""
from agil.store import load_customers
from agil.tools import execute, segment_of


def code(r):
    return None if r["ok"] else r["error"]["code"]


def test_segments_of_the_mock_data():
    seg = {cid: segment_of(c) for cid, c in load_customers().items()}
    assert seg == {"C001": "forgetful", "C002": "temporary_hardship", "C003": "repeat_defaulter",
                   "C004": "in_dispute", "C005": "high_value_b2b", "C006": "temporary_hardship",
                   "C007": "temporary_hardship"}


def test_balance_not_revealed_without_verification(ctx):
    assert code(execute("get_balance", {}, ctx("C001", verified=False))) == "IDENTITY_NOT_VERIFIED"


def test_link_does_not_exceed_the_balance(ctx):
    assert code(execute("create_payment_link", {"amount": 999}, ctx("C001"))) == "INVALID_AMOUNT"
    assert execute("create_payment_link", {"amount": 38.5}, ctx("C001"))["ok"]


def test_promise_max_date_15_days(ctx):
    r = execute("register_payment_promise", {"amount": 142.75, "date": "2026-11-18"}, ctx("C002"))
    assert code(r) == "DATE_OUTSIDE_POLICY" and r["error"]["max_date"] == "2026-11-17"


def test_promise_invalid_date_format(ctx):
    assert code(execute("register_payment_promise", {"amount": 50, "date": "10/11/2026"}, ctx("C002"))) == \
        "INVALID_DATE"


def test_partial_promise_minimum_30_percent(ctx):
    r = execute("register_payment_promise", {"amount": 20, "date": "2026-11-10"}, ctx("C002"))
    assert code(r) == "AMOUNT_OUTSIDE_POLICY" and r["error"]["min_partial_amount"] == 42.83


def test_at_most_one_active_promise(ctx):
    c = ctx("C002")
    assert execute("register_payment_promise", {"amount": 50, "date": "2026-11-10"}, c)["ok"]
    assert code(execute("register_payment_promise", {"amount": 50, "date": "2026-11-11"}, c)) == \
        "ACTIVE_PROMISE_EXISTS"


def test_installments_only_2_or_3_and_add_up_to_the_balance(ctx):
    assert code(execute("offer_payment_plan", {"installments": 6}, ctx("C002"))) == "INSTALLMENTS_NOT_ALLOWED"
    r = execute("offer_payment_plan", {"installments": 3}, ctx("C002"))
    assert r["ok"] and round(sum(i["amount"] for i in r["plan"]), 2) == 142.75


def test_repeat_defaulter_gets_no_plan_or_promise(ctx):
    assert code(execute("offer_payment_plan", {"installments": 2}, ctx("C003"))) == "ACTION_NOT_ALLOWED_FOR_SEGMENT"
    assert code(execute("register_payment_promise", {"amount": 210, "date": "2026-11-05"}, ctx("C003"))) == \
        "ACTION_NOT_ALLOWED_FOR_SEGMENT"


def test_dispute_and_b2b_are_not_collected(ctx):
    assert code(execute("create_payment_link", {"amount": 10}, ctx("C004"))) == "CUSTOMER_IN_DISPUTE"
    assert code(execute("create_payment_link", {"amount": 10}, ctx("C005"))) == "REQUIRES_EXECUTIVE"


def test_llm_cannot_query_another_customer(ctx):
    r = execute("get_balance", {"customer_id": "C005"}, ctx("C001"))
    assert r["name"] == "Ana"


def test_escalation_works_without_verification(ctx):
    r = execute("escalate_to_human", {"reason": "vulnerability", "summary": "x"}, ctx("C007", verified=False))
    assert r["ok"] and r["queue"] == "special_care" and r["sla_hours"] == 2


def test_balance_returns_precomputed_limits(ctx):
    # The LLM does no arithmetic: the 30% minimum and the max date come from code.
    r = execute("get_balance", {}, ctx("C002"))
    assert r["policy_limits"]["min_partial_amount"] == 42.83
    assert r["policy_limits"]["max_promise_date"] == "2026-11-17"


def test_repeat_defaulter_gets_no_promise_limits(ctx):
    assert execute("get_balance", {}, ctx("C003"))["policy_limits"] == {}
