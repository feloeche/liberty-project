"""Las políticas viven en el código de las tools: aquí se prueba que el LLM no las puede saltar."""
from agil.tools import ejecutar, segmentar
from agil.store import cargar_clientes


def codigo(r):
    return None if r["ok"] else r["error"]["codigo"]


def test_segmentos_de_los_datos_mock():
    seg = {cid: segmentar(c) for cid, c in cargar_clientes().items()}
    assert seg == {"C001": "olvidadizo", "C002": "dificultad_temporal", "C003": "reincidente",
                   "C004": "en_disputa", "C005": "alto_valor_b2b", "C006": "dificultad_temporal",
                   "C007": "dificultad_temporal"}


def test_sin_verificacion_no_se_revela_saldo(ctx):
    assert codigo(ejecutar("consultar_saldo", {}, ctx("C001", verificado=False))) == "IDENTIDAD_NO_VERIFICADA"


def test_link_no_supera_el_saldo(ctx):
    assert codigo(ejecutar("generar_link_pago", {"monto": 999}, ctx("C001"))) == "MONTO_INVALIDO"
    assert ejecutar("generar_link_pago", {"monto": 38.5}, ctx("C001"))["ok"]


def test_promesa_fecha_maxima_15_dias(ctx):
    r = ejecutar("registrar_promesa_pago", {"monto": 142.75, "fecha": "2026-11-18"}, ctx("C002"))
    assert codigo(r) == "FECHA_FUERA_DE_POLITICA" and r["error"]["fecha_maxima"] == "2026-11-17"


def test_promesa_parcial_minimo_30_por_ciento(ctx):
    r = ejecutar("registrar_promesa_pago", {"monto": 20, "fecha": "2026-11-10"}, ctx("C002"))
    assert codigo(r) == "MONTO_FUERA_DE_POLITICA" and r["error"]["monto_minimo_parcial"] == 42.83


def test_maximo_una_promesa_activa(ctx):
    c = ctx("C002")
    assert ejecutar("registrar_promesa_pago", {"monto": 50, "fecha": "2026-11-10"}, c)["ok"]
    assert codigo(ejecutar("registrar_promesa_pago", {"monto": 50, "fecha": "2026-11-11"}, c)) == \
        "PROMESA_ACTIVA_EXISTENTE"


def test_cuotas_solo_2_o_3_y_suman_el_saldo(ctx):
    assert codigo(ejecutar("ofrecer_acuerdo_pago", {"cuotas": 6}, ctx("C002"))) == "CUOTAS_NO_PERMITIDAS"
    r = ejecutar("ofrecer_acuerdo_pago", {"cuotas": 3}, ctx("C002"))
    assert r["ok"] and round(sum(c["monto"] for c in r["plan"]), 2) == 142.75


def test_reincidente_sin_acuerdo_ni_promesa(ctx):
    assert codigo(ejecutar("ofrecer_acuerdo_pago", {"cuotas": 2}, ctx("C003"))) == "ACCION_NO_PERMITIDA_SEGMENTO"
    assert codigo(ejecutar("registrar_promesa_pago", {"monto": 210, "fecha": "2026-11-05"}, ctx("C003"))) == \
        "ACCION_NO_PERMITIDA_SEGMENTO"


def test_disputa_y_b2b_no_se_cobran(ctx):
    assert codigo(ejecutar("generar_link_pago", {"monto": 10}, ctx("C004"))) == "CLIENTE_EN_DISPUTA"
    assert codigo(ejecutar("generar_link_pago", {"monto": 10}, ctx("C005"))) == "REQUIERE_EJECUTIVO"


def test_el_llm_no_puede_consultar_otro_cliente(ctx):
    r = ejecutar("consultar_saldo", {"cliente_id": "C005"}, ctx("C001"))
    assert r["nombre"] == "Ana"


def test_escalar_funciona_sin_verificacion(ctx):
    r = ejecutar("escalar_a_humano", {"motivo": "vulnerabilidad", "resumen": "x"}, ctx("C007", verificado=False))
    assert r["ok"] and r["cola"] == "cuidado_especial" and r["sla_horas"] == 2


def test_saldo_entrega_los_limites_ya_calculados(ctx):
    # El LLM no hace aritmética: el mínimo del 30% y la fecha máxima vienen del código.
    r = ejecutar("consultar_saldo", {}, ctx("C002"))
    assert r["limites_politica"]["monto_minimo_parcial"] == 42.83
    assert r["limites_politica"]["fecha_maxima_promesa"] == "2026-11-17"


def test_reincidente_no_recibe_limites_de_promesa(ctx):
    assert ejecutar("consultar_saldo", {}, ctx("C003"))["limites_politica"] == {}
