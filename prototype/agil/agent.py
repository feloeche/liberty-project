"""Orchestrator: one WhatsApp turn end to end.

Flow per inbound message:
  1. Identity verification (code, not the LLM).
  2. Input guardrail -> mandatory escalation?
  3. Tool-use loop: the model decides which tool to call until it gives a final answer.
  4. Escalation guarantee: if a rule required it and the model didn't escalate, code escalates.
  5. Output guardrail -> if the message fails, a safe message is sent and the case is escalated.
In production this is a Lambda / AgentCore Runtime triggered from SQS FIFO."""
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

import anthropic

from . import config, guardrails, tools
from .store import Store, audit

SYSTEM_TAG = "[SYSTEM CONTEXT, not written by the customer]"


@dataclass
class Session:
    """Conversation memory (in production: a DynamoDB item with TTL)."""
    customer_id: str
    verified: bool = False
    insults: int = 0
    case: dict | None = None  # filled on escalation; from then on a human replies
    messages: list[dict] = field(default_factory=list)
    valid_amounts: set[float] = field(default_factory=set)

    def to_dict(self) -> dict:
        """JSON-serializable: on AWS the Lambda is stateless and the session travels to
        DynamoDB between turns (a set is not JSON, so it goes as a list)."""
        return {**asdict(self), "valid_amounts": sorted(self.valid_amounts)}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(**{**d, "valid_amounts": set(d.get("valid_amounts", []))})


@dataclass
class Turn:
    reply: str
    decision: str  # "HANDLED" | "ESCALATED"


class Agent:
    def __init__(self, llm, store: Store, customers: dict[str, dict],
                 on_event: Callable[[dict], None] = lambda e: None):
        self.llm, self.store, self.customers, self.on_event = llm, store, customers, on_event
        self.system = config.PROMPT_PATH.read_text(encoding="utf-8")
        self.sessions: dict[str, Session] = {}

    def respond(self, customer_id: str, text: str) -> Turn:
        s = self.sessions.setdefault(customer_id, Session(customer_id))
        audit("inbound_message", customer_id, text=text)
        template = self._answered_template(s)  # before logging the customer's message
        self.store.save("conversation", {"customer_id": customer_id, "role": "customer", "text": text})
        if s.case:  # a human is already in charge: the bot does not collect again
            return self._close(s, f"Tu caso {s.case['case_id']} ya está con un asesor; te escribirá pronto.")

        if template:
            s.messages += self._template_opening(template)
        self._verify(s, text)
        reason = self._input_guardrail(s, text)
        s.messages.append({"role": "user", "content": [
            {"type": "text", "text": self._context_note(s, reason, template)}, {"type": "text", "text": text}]})

        reply = self._loop(s)
        if reply is None:  # API error, refusal or steps exhausted -> never leave the customer hanging
            reason = reason or "unresolved"
        forced = bool(reason) and not s.case
        if forced:
            self._execute(s, "escalate_to_human", {"reason": reason, "summary": f"Escalation forced by "
                          f"the orchestrator ({reason}). Last message: '{text[:160]}'"})
        if forced or reply is None:
            reply = tools.escalation_text(s.case)
        return self._close(s, self._output_guardrail(s, reply))

    # -- turn steps ---------------------------------------------------------
    def _answered_template(self, s: Session) -> dict | None:
        """If the last thing the customer received was a campaign template, they are
        replying to it. The template enters the model's conversation as its own
        previous message (see _template_opening)."""
        conversation = self.store.find("conversation", s.customer_id)
        last = conversation[-1] if conversation else None
        if last and last["role"] == "campaign":
            self._event(s, "campaign", detail=f"Customer is replying to template {last['template']} "
                                              f"({last['campaign_id']})")
            return last
        return None

    @staticmethod
    def _template_opening(template: dict) -> list[dict]:
        """The model must 'remember' it wrote first. Finding in the demo: with only a
        context note, Claude greeted and introduced itself again, as if the conversation
        were starting from scratch. That's why the template enters as ITS previous
        message (assistant role). The Messages API requires starting with 'user', so it
        is preceded by a system notice explaining where that message came from."""
        notice = (f"{SYSTEM_TAG} LLA started this conversation on WhatsApp with the approved "
                  f"template {template['template']} ({template['campaign_id']}). "
                  f"The next assistant message is exactly what the customer received.")
        return [{"role": "user", "content": [{"type": "text", "text": notice}]},
                {"role": "assistant", "content": [{"type": "text", "text": template["text"]}]}]

    def _verify(self, s: Session, text: str) -> None:
        """Minimal one-step verification: last 4 digits of the national ID. It is
        proportional to the risk (the WhatsApp number is already registered in the CRM)."""
        last4 = re.sub(r"\D", "", self.customers[s.customer_id]["national_id"])[-4:]
        if not s.verified and last4 in re.findall(r"\b\d{4}\b", text):
            s.verified = True
            self._event(s, "verification", detail="identity verified (last 4 digits of the national ID)")

    def _input_guardrail(self, s: Session, text: str) -> str | None:
        reason = guardrails.check_input(text)
        if guardrails.is_insult(text):
            s.insults += 1
            reason = reason or ("legal_or_abuse" if s.insults >= 2 else None)  # 'persistent' = 2+
        if reason:
            self._event(s, "guardrail", detail=f"input -> mandatory escalation: {reason}")
        return reason

    def _context_note(self, s: Session, reason: str | None, template: dict | None = None) -> str:
        """Facts the model needs that come from the system, not from the customer.
        They go in a separate block so the customer's text cannot impersonate it
        (and even if it did, the tools validate against the session, not the note)."""
        note = (f"{SYSTEM_TAG} customer_id={s.customer_id}; "
                f"identity_verified={'yes' if s.verified else 'no'}; today={config.today()}")
        if template:  # no ';' or ']' inside the value: that's how the note is parsed
            note += (f"; replying_to_template={template['template']} sent at {template['created'][:16]}, "
                     f"a notice without amount: the customer does not know the amount or the reason yet")
        return note + (f"; mandatory_escalation={reason}]" if reason else "]")

    def _loop(self, s: Session) -> str | None:
        """Manual agentic loop (not the SDK tool runner) because the offline mock
        must go through exactly the same path as Claude."""
        for _ in range(config.POLICY["max_steps_per_turn"]):
            try:
                r = self.llm.create(self.system, s.messages, tools.SCHEMAS)
            except anthropic.APIError as exc:
                self._event(s, "error", detail=f"model failure: {type(exc).__name__}")
                return None
            s.messages.append({"role": "assistant", "content": r.content})
            if r.stop_reason == "refusal":
                self._event(s, "error", detail="the model declined to answer")
                return None
            uses = [b for b in r.content if b["type"] == "tool_use"]
            if not uses:
                return "".join(b["text"] for b in r.content if b["type"] == "text").strip()
            s.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u["id"], **self._execute(s, u["name"], u["input"])}
                for u in uses]})
        self._event(s, "error", detail="turn steps exhausted")
        return None

    def _execute(self, s: Session, name: str, tool_input: dict) -> dict:
        ctx = tools.Context(self.customers[s.customer_id], self.store, config.today(), s.verified)
        result = tools.execute(name, tool_input, ctx)
        s.valid_amounts |= guardrails.amounts_in(result)
        if name == "escalate_to_human" and result["ok"]:
            s.case = result
        self._event(s, "tool", name=name, input=tool_input, result=result)
        return {"content": tools.result_to_text(result), "is_error": not result["ok"]}

    def _output_guardrail(self, s: Session, reply: str) -> str:
        problems = guardrails.check_output(reply, s.valid_amounts)
        if not problems:
            return reply
        self._event(s, "guardrail", detail=f"output blocked: {'; '.join(problems)}", original=reply)
        if not s.case:  # the safe message promises a human agent: we must deliver
            self._execute(s, "escalate_to_human", {"reason": "unresolved",
                                                   "summary": f"Reply blocked by guardrail: {problems}"})
        return guardrails.SAFE_MESSAGE

    def _close(self, s: Session, reply: str) -> Turn:
        turn = Turn(reply, "ESCALATED" if s.case else "HANDLED")
        self.store.save("conversation", {"customer_id": s.customer_id, "role": "agent", "text": reply,
                                         "decision": turn.decision})
        audit("outbound_message", s.customer_id, text=reply, decision=turn.decision)
        return turn

    def _event(self, s: Session, type_: str, **data) -> None:
        audit(type_, s.customer_id, **data)
        self.on_event({"type": type_, **data})
