import pytest

from agil.guardrails import revisar_entrada, revisar_salida


@pytest.mark.parametrize("texto,motivo", [
    ("Ese cobro de roaming no lo reconozco", "disputa"),
    ("Mi mamá falleció la semana pasada", "vulnerabilidad"),
    ("Quiero hablar con una persona", "solicita_humano"),
    ("Voy a poner una queja en ACODECO", "legal_o_insultos"),
    ("¿Cuánto debo?", None),
])
def test_entrada(texto, motivo):
    assert revisar_entrada(texto) == motivo


def test_salida_bloquea_montos_inventados():
    assert revisar_salida("Tu saldo es USD 38.50", {38.5}) == []
    assert revisar_salida("Tu saldo es USD 99.00", {38.5})


def test_salida_bloquea_amenazas_y_promesas_fuera_de_politica():
    assert revisar_salida("Si no pagas habrá consecuencias legales", set())
    assert revisar_salida("Te puedo dar un descuento del 20%", set())
    assert revisar_salida("Lo dividimos en 6 cuotas", set())


def test_salida_cuotas_fuera_de_politica_solo_si_se_ofrecen():
    # Hallazgo con Claude real: rechazar las 6 cuotas es correcto y no debe bloquearse.
    assert revisar_salida("Sobre las 6 cuotas: lamentablemente no están disponibles.", set()) == []
    assert revisar_salida("Listo, te dejo el pago en 6 cuotas.", set())
