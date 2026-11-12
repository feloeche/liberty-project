import pytest

from agil.guardrails import check_input, check_output


@pytest.mark.parametrize("text,reason", [
    ("Ese cobro de roaming no lo reconozco", "dispute"),
    ("Mi mamá falleció la semana pasada", "vulnerability"),
    ("Quiero hablar con una persona", "human_requested"),
    ("Voy a poner una queja en ACODECO", "legal_or_abuse"),
    ("¿Cuánto debo?", None),
])
def test_input(text, reason):
    assert check_input(text) == reason


def test_output_blocks_made_up_amounts():
    assert check_output("Tu saldo es USD 38.50", {38.5}) == []
    assert check_output("Tu saldo es USD 99.00", {38.5})


def test_output_blocks_threats_and_out_of_policy_promises():
    assert check_output("Si no pagas habrá consecuencias legales", set())
    assert check_output("Te puedo dar un descuento del 20%", set())
    assert check_output("Lo dividimos en 6 cuotas", set())


def test_out_of_policy_installments_only_when_offered():
    # Finding with real Claude: refusing the 6 installments is correct and must not be blocked.
    assert check_output("Sobre las 6 cuotas: lamentablemente no están disponibles.", set()) == []
    assert check_output("Listo, te dejo el pago en 6 cuotas.", set())
