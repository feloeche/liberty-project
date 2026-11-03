"""Extremo a extremo: los escenarios de la demo, offline, sin red."""
from agil.__main__ import ESCENARIOS
from agil.agent import Agente
from agil.llm import MockLLM
from agil.store import Store, cargar_clientes

ESPERADO = {"C001": "GESTIONA", "C002": "GESTIONA", "C003": "ESCALA", "C004": "ESCALA", "C007": "ESCALA"}


def test_escenarios_de_la_demo(tmp_path):
    store, clientes = Store(tmp_path / "store.json"), cargar_clientes()
    for _, cid, mensajes in ESCENARIOS:
        agente = Agente(MockLLM(), store, clientes)
        decisiones = [agente.responder(cid, m).decision for m in mensajes]
        assert decisiones[-1] == ESPERADO[cid], cid
    assert len(store.datos["links"]) == 1 and len(store.datos["promesas"]) == 1
    assert [e["motivo"] for e in store.datos["escalamientos"]] == ["politica_no_permite", "disputa", "vulnerabilidad"]
    assert (tmp_path / "audit.jsonl").exists()


def test_guardrail_de_salida_reemplaza_y_escala(tmp_path):
    class LLMQueAlucina(MockLLM):
        def crear(self, system, messages, tools):
            r = super().crear(system, messages, tools)
            if r.stop_reason == "end_turn":
                r.contenido = [{"type": "text", "text": "Debes USD 999.99 o te vamos a reportar"}]
            return r

    store = Store(tmp_path / "store.json")
    t = Agente(LLMQueAlucina(), store, cargar_clientes()).responder("C001", "Cédula 4521, ¿cuánto debo?")
    assert t.decision == "ESCALA" and "asesor" in t.respuesta and "999" not in t.respuesta
