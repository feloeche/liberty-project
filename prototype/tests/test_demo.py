"""End to end: the demo scenarios, offline, without network."""
from agil.__main__ import SCENARIOS
from agil.agent import Agent
from agil.llm import MockLLM
from agil.store import Store, load_customers

EXPECTED = {"C001": "HANDLED", "C002": "HANDLED", "C003": "ESCALATED", "C004": "ESCALATED", "C007": "ESCALATED"}


def test_demo_scenarios(tmp_path):
    store, customers = Store(tmp_path / "store.json"), load_customers()
    for _, cid, messages in SCENARIOS:
        agent = Agent(MockLLM(), store, customers)
        decisions = [agent.respond(cid, m).decision for m in messages]
        assert decisions[-1] == EXPECTED[cid], cid
    assert len(store.data["links"]) == 1 and len(store.data["promises"]) == 1
    assert [e["reason"] for e in store.data["escalations"]] == ["policy_not_allowed", "dispute", "vulnerability"]
    assert (tmp_path / "audit.jsonl").exists()


def test_output_guardrail_replaces_and_escalates(tmp_path):
    class HallucinatingLLM(MockLLM):
        def create(self, system, messages, tools):
            r = super().create(system, messages, tools)
            if r.stop_reason == "end_turn":
                r.content = [{"type": "text", "text": "Debes USD 999.99 o te vamos a reportar"}]
            return r

    store = Store(tmp_path / "store.json")
    t = Agent(HallucinatingLLM(), store, load_customers()).respond("C001", "Cédula 4521, ¿cuánto debo?")
    assert t.decision == "ESCALATED" and "asesor" in t.reply and "999" not in t.reply
