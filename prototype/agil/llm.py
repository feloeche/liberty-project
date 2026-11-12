"""The 'brain' behind a minimal interface: create(system, messages, tools).

Three implementations with the SAME interface (Messages API-style content
blocks: text / tool_use), so the agent, the tools and the guardrails neither
know nor care who decides:
- ClaudeLLM(bedrock=False): Claude API with the official Anthropic SDK.
- ClaudeLLM(bedrock=True):  Amazon Bedrock (the production path on AWS).
- MockLLM: deterministic rule engine to run without credentials. It is NOT
  an LLM: it imitates its decisions so the demo is reproducible. It parses the
  customer's Spanish and replies in Spanish, like the real agent."""
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import anthropic

from . import config
from .tools import escalation_text


@dataclass
class Response:
    content: list[dict]
    stop_reason: str


class ClaudeLLM:
    def __init__(self, bedrock: bool = False):
        # Per-call timeout: on AWS the HTTP API cuts off at 30 s, so it's better to
        # fail fast and let the orchestrator escalate than leave the customer without a reply.
        # max_retries=3: the demo account has a 10 requests/min limit on Bedrock;
        # the SDK retries 429s (RateLimitError) with exponential backoff.
        options = {"timeout": config.LLM_TIMEOUT_S, "max_retries": 3}
        if bedrock:
            # bedrock-runtime InvokeModel signed with IAM (the Lambda role). This client is
            # used instead of AnthropicBedrockMantle because it accepts the cross-region
            # inference profiles (us.anthropic.*) enabled on the account and is authorized
            # with bedrock:InvokeModel, the standard IAM action.
            self.client = anthropic.AnthropicBedrock(aws_region=config.AWS_REGION, **options)
            self.model, self.label = config.BEDROCK_MODEL, f"Amazon Bedrock · {config.BEDROCK_MODEL}"
        else:
            self.client = anthropic.Anthropic(**options)
            self.model, self.label = config.MODEL, f"Claude API · {config.MODEL}"
        self.extra = {"output_config": {"effort": config.EFFORT}}

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Response:
        try:
            r = self.client.messages.create(model=self.model, max_tokens=8000, system=system,
                                            tools=tools, messages=messages, **self.extra)
        except anthropic.BadRequestError as exc:
            if not self.extra or ("output_config" not in str(exc) and "effort" not in str(exc)):
                raise
            # Graceful degradation: if the endpoint/model doesn't accept `effort`, carry
            # on with the model's default effort instead of escalating everything.
            self.extra = {}
            return self.create(system, messages, tools)
        # The full content is returned (including thinking blocks) so it can be sent
        # back intact in the next request, as the API requires.
        return Response([b.model_dump(exclude_none=True) for b in r.content], r.stop_reason)


def create_llm():
    """Picks the brain according to config.provider() (LLM_PROVIDER / ANTHROPIC_API_KEY)."""
    p = config.provider()
    return MockLLM() if p == "mock" else ClaudeLLM(bedrock=(p == "bedrock"))


# ---------------------------------------------------------------- offline mode
@dataclass
class _Call:
    name: str
    input: dict
    result: dict


WEEKDAYS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _amount(t: str) -> float | None:
    m = re.search(r"(?:\$|usd|b/\.)\s?(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:dolares|balboas)", t)
    return float(m.group(1) or m.group(2)) if m else None


def _date(t: str, today: date) -> date | None:
    if m := re.search(r"en (\d+) dias", t):
        return today + timedelta(days=int(m.group(1)))
    if m := re.search(r"\d{4}-\d{2}-\d{2}", t):
        return date.fromisoformat(m.group(0))
    if "manana" in t:
        return today + timedelta(days=1)
    for i, day in enumerate(WEEKDAYS_ES):
        if f"el {day}" in t:
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return None


class MockLLM:
    label = "OFFLINE · motor de reglas determinístico (sin LLM, sin red)"

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Response:
        decision = self._decide(*self._read(messages))
        if isinstance(decision, str):
            return Response([{"type": "text", "text": decision}], "end_turn")
        name, tool_input = decision
        block = {"type": "tool_use", "id": f"toolu_mock_{uuid.uuid4().hex[:10]}",
                 "name": name, "input": tool_input}
        return Response([block], "tool_use")

    @staticmethod
    def _read(messages: list[dict]):
        """Rebuilds what an LLM would 'see': the customer's last text, the system
        context note, and every tool already executed (with its result)."""
        from .guardrails import normalize
        uses, calls, turn_start, text, context = {}, [], 0, "", {}
        for msg in messages:
            for b in msg["content"]:
                if b["type"] == "tool_use":
                    uses[b["id"]] = b
                elif b["type"] == "tool_result":
                    u = uses[b["tool_use_id"]]
                    calls.append(_Call(u["name"], u["input"], json.loads(b["content"])))
            texts = [b["text"] for b in msg["content"] if b["type"] == "text"]
            if msg["role"] == "user" and texts:
                context = dict(re.findall(r"(\w+)=([^;\]]+)", texts[0]))
                text, turn_start = normalize(texts[-1]), len(calls)
        return text, context, calls, calls[turn_start:]

    def _decide(self, text, ctx, calls, this_turn):
        cid = ctx["customer_id"]
        last = this_turn[-1] if this_turn else None
        if last and last.name == "escalate_to_human" and last.result["ok"]:
            return escalation_text(last.result)
        if reason := ctx.get("mandatory_escalation"):
            return self._escalate(cid, reason, text)
        if ctx.get("identity_verified") != "yes":
            return ("Hola, soy el asistente virtual de LLA. Para proteger tus datos, "
                    "¿me confirmas los últimos 4 dígitos de tu cédula?")
        facts = {c.name: c.result for c in calls if c.result["ok"]}
        for lookup in ("get_balance", "get_history"):
            if lookup not in facts:
                return lookup, {"customer_id": cid}
        account = facts["get_balance"]
        if last and last.name not in ("get_balance", "get_history"):
            return self._react(cid, last, calls, account, text)
        if account["segment"] in ("in_dispute", "high_value_b2b"):
            reason = "dispute" if account["segment"] == "in_dispute" else "high_value_b2b"
            return self._escalate(cid, reason, text)
        return self._intent(cid, text, account, date.fromisoformat(ctx["today"]))

    @staticmethod
    def _escalate(cid, reason, text):
        return "escalate_to_human", {"customer_id": cid, "reason": reason,
                                     "summary": f"Reason: {reason}. Customer's last message: '{text[:160]}'"}

    def _intent(self, cid, text, account, today):
        """Maps what the customer asks for to ONE tool. The tool decides if it's allowed."""
        balance = account["overdue_balance"]
        if m := re.search(r"(\d+)\s*cuotas", text):
            return "offer_payment_plan", {"customer_id": cid, "installments": int(m.group(1))}
        if re.search(r"acuerdo|en partes|cuotas", text):
            return "offer_payment_plan", {"customer_id": cid, "installments": 3}
        if day := _date(text, today):
            return "register_payment_promise", {"customer_id": cid, "amount": _amount(text) or balance,
                                                "date": str(day)}
        if re.search(r"pagar|pago|link|enlace", text):
            return "create_payment_link", {"customer_id": cid, "amount": _amount(text) or balance}
        if re.search(r"gracias|listo|perfecto", text):
            return "¡Gracias a ti! Si necesitas algo más, escríbeme por aquí. Que tengas buen día."
        options = {"create_payment_link": "pagar ahora con un link seguro",
                   "register_payment_promise": "acordar una fecha de pago (hasta 15 días)",
                   "offer_payment_plan": "dividirlo en 2 o 3 cuotas"}
        listing = "; ".join(options[a] for a in account["allowed_actions"] if a in options)
        return (f"Gracias, {account['name']}. Tienes un saldo vencido de USD {balance:.2f} "
                f"con {account['days_past_due']} días de atraso. Puedo ayudarte a: {listing}. ¿Qué prefieres?")

    def _react(self, cid, last, calls, account, text):
        """Reply to the result of the tool that was just executed."""
        r = last.result
        if r["ok"]:
            if last.name == "create_payment_link":
                return (f"Listo. Aquí tienes tu link para pagar USD {r['amount']:.2f}: {r['url']} "
                        f"(válido hasta {r['expires_on']}). Tu servicio se normaliza al confirmarse el pago.")
            if last.name == "register_payment_promise":
                return (f"Perfecto, registré tu compromiso de pagar USD {r['amount']:.2f} el {r['date']}. "
                        f"Te enviaré un recordatorio ese día. ¡Gracias por tu disposición!")
            installments = ", ".join(f"USD {i['amount']:.2f} el {i['date']}" for i in r["plan"])
            return f"Listo, tu acuerdo quedó registrado en {len(r['plan'])} cuotas: {installments}."
        code = r["error"]["code"]
        rejections = sum(not c.result["ok"] and c.result["error"]["code"] in
                         ("INSTALLMENTS_NOT_ALLOWED", "PLAN_NOT_AVAILABLE", "ACTION_NOT_ALLOWED_FOR_SEGMENT")
                         for c in calls)
        if code in ("CUSTOMER_IN_DISPUTE", "REQUIRES_EXECUTIVE"):
            return self._escalate(cid, "dispute" if code == "CUSTOMER_IN_DISPUTE" else "high_value_b2b", text)
        if rejections >= 2:  # the customer insists on something policy doesn't allow
            return self._escalate(cid, "policy_not_allowed", text)
        if code == "DATE_OUTSIDE_POLICY":
            e = r["error"]
            return (f"Entiendo. Puedo registrar tu compromiso con fecha máxima {e['max_date']}: "
                    f"por el total (USD {e['overdue_balance']:.2f}) o un abono desde "
                    f"USD {e['min_partial_amount']:.2f}. ¿Qué monto y fecha te funcionan?")
        if code == "AMOUNT_OUTSIDE_POLICY":
            return (f"Para un abono parcial el mínimo es USD {r['error']['min_partial_amount']:.2f}. "
                    f"¿Te funciona ese monto?")
        if code in ("PLAN_NOT_AVAILABLE", "ACTION_NOT_ALLOWED_FOR_SEGMENT"):
            return (f"{account['name']}, en tu caso no puedo ofrecer un acuerdo en cuotas. Lo que sí "
                    f"puedo hacer hoy es enviarte un link para pagar el total de USD "
                    f"{account['overdue_balance']:.2f} y reactivar tu servicio. ¿Te lo envío?")
        if code == "INSTALLMENTS_NOT_ALLOWED":
            return "Puedo ofrecerte el saldo en 2 o 3 cuotas. ¿Cuál prefieres?"
        if code == "ACTIVE_PROMISE_EXISTS":
            return "Ya tienes un compromiso de pago activo; te recordaré la fecha acordada."
        return self._escalate(cid, "unresolved", text)  # unexpected error: better a human
