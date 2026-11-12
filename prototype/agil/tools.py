"""Agent tools (English equivalents of the tools in the architecture proposal, §5.3).

Core decision: policies (amounts, deadlines, installments, who may negotiate) are
validated HERE, in deterministic code. The LLM only proposes; if it proposes something
outside policy it gets a structured error with alternatives and must steer the
conversation back. That way a prompt injection or a hallucination cannot
"give away" 6 installments or a date 60 days out."""
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from .config import POLICY
from .store import Store

ESCALATION_REASONS = ["dispute", "vulnerability", "human_requested", "legal_or_abuse",
                      "high_value_b2b", "policy_not_allowed", "unresolved"]

# What the agent may offer per segment (architecture proposal, §1.2).
ACTIONS = {
    "forgetful": ["create_payment_link", "register_payment_promise"],
    "temporary_hardship": ["create_payment_link", "register_payment_promise", "offer_payment_plan"],
    "repeat_defaulter": ["create_payment_link"],
    "in_dispute": ["escalate_to_human"],
    "high_value_b2b": ["escalate_to_human"],
}

QUEUES = {"dispute": "claims", "high_value_b2b": "b2b_executive", "vulnerability": "special_care"}

_ID = {"type": "string", "description": "Internal customer ID, e.g. C001"}
SCHEMAS = [
    {"name": "get_balance",
     "description": "Overdue balance, days past due, invoices, services, status and segment of the customer. "
                    "Requires verified identity.",
     "input_schema": {"type": "object", "properties": {"customer_id": _ID}, "required": ["customer_id"]}},
    {"name": "get_history",
     "description": "Previous promises (kept/broken), recent contacts and open claims.",
     "input_schema": {"type": "object", "properties": {"customer_id": _ID}, "required": ["customer_id"]}},
    {"name": "create_payment_link",
     "description": "Creates a payment link for an amount in USD (greater than 0 and at most the balance).",
     "input_schema": {"type": "object", "properties": {"customer_id": _ID, "amount": {"type": "number"}},
                      "required": ["customer_id", "amount"]}},
    {"name": "register_payment_promise",
     "description": "Registers a promise to pay. Policies: date at most 15 days out, "
                    "if partial at least 30% of the balance, at most 1 active promise.",
     "input_schema": {"type": "object", "properties": {
         "customer_id": _ID, "amount": {"type": "number"},
         "date": {"type": "string", "description": "Date YYYY-MM-DD"}},
         "required": ["customer_id", "amount", "date"]}},
    {"name": "offer_payment_plan",
     "description": "Registers an installment plan (only 2 or 3) if the campaign and the segment allow it.",
     "input_schema": {"type": "object", "properties": {"customer_id": _ID, "installments": {"type": "integer"}},
                      "required": ["customer_id", "installments"]}},
    {"name": "escalate_to_human",
     "description": "Creates a case in the human queue with a summary of the conversation. "
                    "Always use it when a mandatory escalation rule applies.",
     "input_schema": {"type": "object", "properties": {
         "customer_id": _ID, "reason": {"type": "string", "enum": ESCALATION_REASONS},
         "summary": {"type": "string"}}, "required": ["customer_id", "reason", "summary"]}},
]


@dataclass
class Context:
    """What the tool knows besides its arguments. 'verified' comes from the
    session (code), never from what the LLM says."""
    customer: dict
    store: Store
    today: date
    verified: bool


def ok(**data) -> dict:
    return {"ok": True, **data}


def error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


NOT_VERIFIED = ("IDENTITY_NOT_VERIFIED", "Ask for the last 4 digits of the national ID (cédula) before continuing.")


def balance_of(customer: dict) -> float:
    return round(sum(i["amount"] for i in customer["overdue_invoices"]), 2)


def segment_of(customer: dict) -> str:
    """Simple, explainable rules, in priority order. Phase 2: payment propensity
    scoring (SageMaker) instead of fixed thresholds."""
    broken = sum(p["status"] == "broken" for p in customer["history"]["promises"])
    if customer["type"] == "B2B" or balance_of(customer) > POLICY["human_threshold_usd"]:
        return "high_value_b2b"
    if customer["history"]["open_claims"]:
        return "in_dispute"
    if broken >= 2:
        return "repeat_defaulter"
    if customer["days_past_due"] <= 30 and broken == 0:
        return "forgetful"
    return "temporary_hardship"


def _bucket(days: int) -> str:
    return "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"


def _collection_block(ctx: Context, action: str) -> dict | None:
    """Barriers shared by every tool that collects or negotiates."""
    if not ctx.verified:
        return error(*NOT_VERIFIED)
    segment = segment_of(ctx.customer)
    if segment == "in_dispute":
        return error("CUSTOMER_IN_DISPUTE", "There is an open claim: do not collect, escalate.",
                     alternatives=["escalate_to_human"])
    if segment == "high_value_b2b":
        return error("REQUIRES_EXECUTIVE", "B2B customer or high amount: handled by an account executive.",
                     alternatives=["escalate_to_human"])
    if action not in ACTIONS[segment]:
        return error("ACTION_NOT_ALLOWED_FOR_SEGMENT",
                     f"Segment '{segment}' does not allow {action}.", alternatives=ACTIONS[segment])
    return None


def get_balance(ctx: Context) -> dict:
    """Data minimization (Panama Law 81 of 2019): returns neither national ID nor phone."""
    if not ctx.verified:
        return error(*NOT_VERIFIED)
    c = ctx.customer
    segment = segment_of(c)
    return ok(name=c["name"].split()[0], overdue_balance=balance_of(c), days_past_due=c["days_past_due"],
              delinquency_bucket=_bucket(c["days_past_due"]), invoices=c["overdue_invoices"],
              services=c["services"], service_status=c["service_status"], type=c["type"],
              segment=segment, allowed_actions=ACTIONS[segment],
              policy_limits=_limits(ctx, segment))


def _limits(ctx: Context, segment: str) -> dict:
    """Policy numbers already computed by code. Finding with the real LLM: Claude
    computed the 30% itself (USD 42.83) and the amount guardrail rightly blocked it,
    because that number didn't come from a tool. If code hands over the computation,
    the model does no arithmetic and every amount is backed."""
    balance, limits = balance_of(ctx.customer), {}
    if "register_payment_promise" in ACTIONS[segment]:  # only what the segment can use
        limits["min_partial_amount"] = math.ceil(balance * POLICY["min_partial_pct"] * 100) / 100
        limits["max_promise_date"] = str(ctx.today + timedelta(days=POLICY["max_promise_days"]))
    if "offer_payment_plan" in ACTIONS[segment] and ctx.customer["campaign_allows_plan"]:
        limits["allowed_installments"] = {n: math.floor(balance / n * 100) / 100
                                          for n in POLICY["allowed_installments"]}
    return limits


def get_history(ctx: Context) -> dict:
    if not ctx.verified:
        return error(*NOT_VERIFIED)
    h = ctx.customer["history"]
    new = ctx.store.find("promises", ctx.customer["customer_id"])
    return ok(promises=h["promises"] + new, recent_contacts=h["recent_contacts"],
              open_claims=h["open_claims"])


def create_payment_link(ctx: Context, amount: float) -> dict:
    if block := _collection_block(ctx, "create_payment_link"):
        return block
    balance = balance_of(ctx.customer)
    if not 0 < amount <= balance:
        return error("INVALID_AMOUNT", "The amount must be greater than 0 and not exceed the balance.",
                     overdue_balance=balance)
    link = ctx.store.save("links", {
        "customer_id": ctx.customer["customer_id"], "amount": round(amount, 2),
        "url": f"https://pagos.lla.example/pa/{uuid.uuid4().hex[:8]}",
        "expires_on": str(ctx.today + timedelta(days=2))})
    return ok(**link)


def register_payment_promise(ctx: Context, amount: float, date: str) -> dict:
    if block := _collection_block(ctx, "register_payment_promise"):
        return block
    cid, balance = ctx.customer["customer_id"], balance_of(ctx.customer)
    minimum = math.ceil(balance * POLICY["min_partial_pct"] * 100) / 100
    latest = ctx.today + timedelta(days=POLICY["max_promise_days"])
    try:
        day = _parse_date(date)
    except ValueError:
        return error("INVALID_DATE", "Use the YYYY-MM-DD format.")
    if not ctx.today < day <= latest:
        return error("DATE_OUTSIDE_POLICY", f"The date must be between tomorrow and {latest}.",
                     max_date=str(latest), overdue_balance=balance, min_partial_amount=minimum)
    if not minimum <= amount <= balance:
        return error("AMOUNT_OUTSIDE_POLICY", "A partial payment must be at least 30% of the balance.",
                     min_partial_amount=minimum, overdue_balance=balance)
    if any(p["status"] == "active" for p in ctx.store.find("promises", cid)):
        return error("ACTIVE_PROMISE_EXISTS", "This customer already has an active promise.")
    promise = ctx.store.save("promises", {"customer_id": cid, "amount": round(amount, 2),
                                          "date": str(day), "status": "active"})
    return ok(**promise, remaining_balance=round(balance - amount, 2))


def _parse_date(value: str):
    # The tool argument is called `date`, which shadows the datetime.date class inside
    # register_payment_promise; parsing lives here to keep that signature clean.
    return date.fromisoformat(value)


def offer_payment_plan(ctx: Context, installments: int) -> dict:
    if block := _collection_block(ctx, "offer_payment_plan"):
        return block
    cid, balance = ctx.customer["customer_id"], balance_of(ctx.customer)
    if not ctx.customer["campaign_allows_plan"]:
        return error("PLAN_NOT_AVAILABLE", "The current campaign does not allow payment plans.",
                     alternatives=["create_payment_link", "register_payment_promise"])
    if installments not in POLICY["allowed_installments"]:
        return error("INSTALLMENTS_NOT_ALLOWED", "Only 2 or 3 installments are allowed.",
                     allowed_installments=list(POLICY["allowed_installments"]))
    if ctx.store.find("plans", cid):
        return error("PLAN_EXISTS", "The customer already has a registered payment plan.")
    base = math.floor(balance / installments * 100) / 100  # the last installment absorbs the cents
    plan = [{"installment": i + 1, "date": str(ctx.today + timedelta(days=30 * i + 7)),
             "amount": base if i < installments - 1 else round(balance - base * (installments - 1), 2)}
            for i in range(installments)]
    return ok(**ctx.store.save("plans", {"customer_id": cid, "total": balance, "plan": plan}))


def escalate_to_human(ctx: Context, reason: str, summary: str) -> dict:
    """Always available (even without verified identity): escalating must never fail."""
    if reason not in ESCALATION_REASONS:
        reason = "unresolved"
    case = ctx.store.save("escalations", {
        "customer_id": ctx.customer["customer_id"], "case_id": f"CASE-{ctx.store.count('escalations') + 1:04d}",
        "reason": reason, "queue": QUEUES.get(reason, "human_collections"),
        "sla_hours": 2 if reason == "vulnerability" else 24,
        # Data minimization (Law 81/2019): the human agent doesn't need the ID digits.
        "summary": re.sub(r"\d{4,}", "****", summary)})
    return ok(**case)


_TOOLS = {f.__name__: f for f in (get_balance, get_history, create_payment_link,
                                    register_payment_promise, offer_payment_plan, escalate_to_human)}


def execute(name: str, args: dict, ctx: Context) -> dict:
    """Single execution point: ignores the customer_id sent by the LLM and uses the
    session's, so the model cannot look up another customer."""
    function = _TOOLS.get(name)
    if function is None:
        return error("UNKNOWN_TOOL", f"Tool {name} does not exist.")
    args = {k: v for k, v in args.items() if k != "customer_id"}
    try:
        return function(ctx, **args)
    except (TypeError, ValueError) as exc:
        return error("INVALID_ARGUMENTS", str(exc))


def escalation_text(case: dict) -> str:
    """Handoff message (in Spanish, for the customer): honest (says a person takes over)
    and with the case number."""
    opening = {
        "dispute": "Entiendo tu inquietud con ese cobro. No te pediremos ese pago mientras se revisa.",
        "vulnerability": "Lamento mucho lo que estás pasando. Tu tranquilidad es lo primero.",
        "human_requested": "Con gusto te comunico con una persona de nuestro equipo.",
        "high_value_b2b": "Por el tipo de cuenta, tu caso lo atiende un ejecutivo dedicado.",
        "policy_not_allowed": "Entiendo tu situación; esa opción no la puedo autorizar yo.",
    }.get(case["reason"], "Quiero asegurarme de darte la mejor solución.")
    return (f"{opening} He pasado tu caso a un asesor de LLA (caso {case['case_id']}), "
            f"quien te contactará en menos de {case['sla_hours']} horas por este mismo chat.")


def result_to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
