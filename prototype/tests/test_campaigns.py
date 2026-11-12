"""Contacto proactivo: las reglas de supresión son código determinístico y se prueban una por una."""
import json
import re
from datetime import datetime

import pytest

from agil import api, campanas
from agil.campanas import PANAMA, ejecutar_campana, motivo_supresion
from agil.store import Store, cargar_clientes

LUNES_10 = datetime(2026, 11, 2, 10, 0, tzinfo=PANAMA)  # HOY de conftest es lunes


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store.json")


@pytest.fixture
def clientes():
    return cargar_clientes()


def motivo(cliente, store, ahora=LUNES_10, forzar=False):
    r = motivo_supresion(cliente, store, ahora, forzar)
    return r and r[0]


def test_resultado_de_la_campana_con_los_datos_de_demo(clientes, store):
    r = ejecutar_campana(clientes, store, ahora=LUNES_10)
    estados = {f["cliente_id"]: f.get("motivo", f["estado"]) for f in r["resultados"]}
    assert estados == {"C001": "enviado", "C002": "enviado", "C003": "contactado_recientemente",
                       "C004": "reclamo_abierto", "C005": "segmento_no_cobrable", "C006": "sin_opt_in",
                       "C007": "enviado"}
    assert r["campana_id"] == "CMP-20261102-0001" and r["resumen"]["enviados"] == 3
    assert len(store.datos["contactos"]) == 3 and store.datos["campanas"][0]["resumen"] == r["resumen"]


@pytest.mark.parametrize("cambio, esperado", [
    ({"opt_in": False}, "sin_opt_in"),
    ({"opt_out": True}, "opt_out"),
    ({"opt_in": False, "opt_out": True}, "sin_opt_in"),  # el primero que aplica gana
    ({"facturas_vencidas": []}, "sin_saldo"),
])
def test_motivos_por_datos_del_cliente(clientes, store, cambio, esperado):
    assert motivo({**clientes["C001"], **cambio}, store) == esperado


def test_motivos_por_estado_del_store(clientes, store):
    store.guardar("promesas", {"cliente_id": "C002", "monto": 50, "estado": "activa"})
    assert motivo(clientes["C002"], store) == "promesa_vigente"
    store.guardar("escalamientos", {"cliente_id": "C002", "caso_id": "CASO-0001"})
    assert motivo(clientes["C002"], store) == "caso_humano_abierto"  # va antes que la promesa


@pytest.mark.parametrize("ahora, permitido", [
    (LUNES_10, True),
    (datetime(2026, 11, 1, 10, 0, tzinfo=PANAMA), False),  # domingo
    (datetime(2026, 11, 2, 21, 0, tzinfo=PANAMA), False),  # 21:00
    (datetime(2026, 11, 2, 7, 59, tzinfo=PANAMA), False),
])
def test_horario(clientes, store, ahora, permitido):
    assert (motivo(clientes["C001"], store, ahora) != "fuera_de_horario") is permitido
    assert motivo(clientes["C001"], store, ahora, forzar=True) != "fuera_de_horario"


def test_consentimiento_se_evalua_antes_que_el_horario(clientes, store):
    domingo = datetime(2026, 11, 1, 10, 0, tzinfo=PANAMA)
    assert motivo(clientes["C006"], store, domingo) == "sin_opt_in"


def test_regla_de_3_dias(clientes, store):
    ejecutar_campana(clientes, store, ["olvidadizo"], ahora=LUNES_10)
    assert motivo(clientes["C001"], store) == "contactado_recientemente"
    jueves = datetime(2026, 11, 5, 10, 0, tzinfo=PANAMA)  # 3 días después: ya se puede
    assert motivo(clientes["C001"], store, jueves) is None


def test_la_plantilla_no_revela_montos(clientes, store):
    """Si el número fue reasignado, la plantilla la lee un tercero: no debe mostrar la deuda."""
    r = ejecutar_campana(clientes, store, ahora=LUNES_10)
    for f in (f for f in r["resultados"] if f["estado"] == "enviado"):
        assert not re.search(r"\d|usd|\$|saldo|deuda|mora", f["texto"], re.I), f["texto"]
        assert f["texto"].startswith(f"Hola {clientes[f['cliente_id']]['nombre'].split()[0]},")


# -- por HTTP: el mismo handle() que corre en Lambda --------------------------
@pytest.fixture
def http(monkeypatch):
    api._contexto.cache_clear()
    monkeypatch.delenv("AGIL_DEMO_TOKEN", raising=False)
    monkeypatch.setattr(campanas, "ahora_panama", lambda: LUNES_10)

    def llamar(method, path, body=None, query=None):
        status, _, salida = api.handle(method, path, query or {}, {},
                                       json.dumps(body).encode() if body is not None else b"")
        return status, json.loads(salida)
    yield llamar
    api._contexto.cache_clear()


def test_endpoints_de_campana_y_conversacion(http):
    _, seg = http("GET", "/api/campanas/segmentos")
    assert {s["id"]: s["elegibles_estimados"] for s in seg["segmentos"]}["dificultad_temporal"] == 3
    assert seg["plantilla"]["categoria"] == "UTILITY"
    assert http("POST", "/api/campanas/ejecutar", {"segmentos": ["vip"]})[0] == 400
    _, r = http("POST", "/api/campanas/ejecutar", {"segmentos": ["olvidadizo", "en_disputa"]})
    assert [(f["cliente_id"], f["estado"]) for f in r["resultados"]] == [("C001", "enviado"), ("C004", "suprimido")]

    # El cliente responde: el primer evento del turno es 'campana' y la UI ve el historial completo.
    _, t = http("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Hola, ¿qué aviso? Cédula 4521"})
    assert t["eventos"][0] == {"tipo": "campana", "detalle": "El cliente responde a la plantilla "
                                                             f"aviso_cuenta_v1 ({r['campana_id']})"}
    _, t2 = http("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Mándame el link"})
    assert all(e["tipo"] != "campana" for e in t2["eventos"])  # solo al responder a la plantilla
    _, conv = http("GET", "/api/conversacion", query={"cliente_id": "C001"})
    assert [m["rol"] for m in conv["mensajes"]] == ["campana", "cliente", "agente", "cliente", "agente"]
    assert conv["mensajes"][0]["campana_id"] == r["campana_id"] and conv["mensajes"][2]["decision"] == "GESTIONA"

    _, op = http("GET", "/api/operacion")
    assert op["campanas"][0]["campana_id"] == r["campana_id"] and op["contactos"][0]["nombre"] == "Ana Rodríguez"
    _, cl = http("GET", "/api/clientes")
    por_id = {c["cliente_id"]: c for c in cl["clientes"]}
    assert por_id["C006"]["opt_in"] is False and por_id["C004"]["ultimo_contacto"] is None
    assert por_id["C001"]["ultimo_contacto"].startswith("20")


def test_el_modelo_ve_la_plantilla_como_su_mensaje_anterior(http, monkeypatch):
    """Hallazgo de la demo: con solo una nota, Claude volvía a saludar. La plantilla entra
    como mensaje 'assistant', precedida de un aviso 'user' (la API exige empezar con 'user')."""
    http("POST", "/api/campanas/ejecutar", {"segmentos": ["olvidadizo"]})
    http("POST", "/api/mensaje", {"cliente_id": "C001", "texto": "Hola"})
    aviso, plantilla, turno = api._contexto()[2].cargar("C001")["mensajes"][:3]
    assert aviso["role"] == "user" and "aviso_cuenta_v1" in aviso["content"][0]["text"]
    assert plantilla["role"] == "assistant" and plantilla["content"][0]["text"].startswith("Hola Ana")
    assert turno["role"] == "user" and "responde_a_plantilla=aviso_cuenta_v1" in turno["content"][0]["text"]


def test_evento_del_scheduler_en_lambda(http):
    assert api.lambda_handler({"accion": "campana_diaria"}, None)["enviados"] == 3
