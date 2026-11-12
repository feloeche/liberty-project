"""Deterministic guardrails, before and after the LLM.

They are deliberately simple rules (keywords / regex): they can be read, audited
and explained to Legal and Compliance. They don't replace Bedrock Guardrails in
production; they are the cheap, predictable first layer. When in doubt, they escalate:
a false positive costs an agent one minute, a false negative costs a vulnerable
customer being pressured or a complaint before ACODECO.

The patterns are in Spanish on purpose: they match what Panamanian customers write
and what the agent replies."""
import re
import unicodedata

# Input: if any rule fires, escalation is MANDATORY.
INPUT_RULES = {
    "dispute": r"no reconozco|no lo reconozco|cobro indebido|cobro injusto|ya pague|no debo|"
               r"error en (la|mi) factura|reclamo|me estan cobrando de mas|no es mio",
    "vulnerability": r"hospital|enferm|cancer|quimio|fallec|murio|luto|funeral|duelo|"
                     r"perdi (mi|el) (trabajo|empleo)|desemplead|no tengo (para|que) comer|"
                     r"deprimid|no aguanto mas|quiero morir",
    "human_requested": r"hablar con (una persona|un humano|alguien|un asesor|un agente)|"
                       r"\b(asesor|humano|persona real|operador)\b",
    "legal_or_abuse": r"acodeco|abogado|demanda|denuncia|tribunal|defensoria",
}
INSULTS = r"\b(idiota|estupid|imbecil|ladron|estafador|maldit|basura|mierda)"

# Output: what the agent must never say.
THREATS = r"embarg|carcel|policia|demandar(emos|te)|abogados|te vamos a reportar|consecuencias legales"
OUT_OF_POLICY = r"descuento|condon|perdonar (la|tu) deuda|exoner|sin recargo"
FORBIDDEN_INSTALLMENTS = r"\b([4-9]|1\d)\s*cuotas"
# Finding with the real LLM: "sobre las 6 cuotas, no están disponibles" is a correct
# REFUSAL and the previous rule blocked it. It is evaluated per sentence.
NEGATION = r"\b(no|ni|solo|unicamente|lamentablemente|lamento|desafortunadamente)\b"
AMOUNT = r"(?:US\$|USD|B/\.|\$)\s?(\d[\d,]*(?:\.\d{1,2})?)"

SAFE_MESSAGE = ("Gracias por tu mensaje. Para darte información precisa, un asesor de LLA "
                "revisará tu caso y te escribirá por este chat.")


def normalize(text: str) -> str:
    """Lowercase and no accents: 'Falleció' and 'fallecio' must fire the same rule."""
    no_accents = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return no_accents.lower()


def check_input(text: str) -> str | None:
    """Returns the mandatory escalation reason, or None."""
    t = normalize(text)
    return next((reason for reason, pattern in INPUT_RULES.items() if re.search(pattern, t)), None)


def is_insult(text: str) -> bool:
    return bool(re.search(INSULTS, normalize(text)))


def amounts_in(obj) -> set[float]:
    """Every number returned by the tools: it is the amount 'allowlist'."""
    if isinstance(obj, bool):
        return set()
    if isinstance(obj, (int, float)):
        return {round(float(obj), 2)}
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        return set().union(*(amounts_in(x) for x in obj)) if obj else set()
    return set()


def check_output(text: str, valid_amounts: set[float]) -> list[str]:
    """Problems found in the outgoing message (empty = OK to send).
    The amount rule targets the worst possible hallucination in collections: a made-up amount."""
    t = normalize(text)
    problems = []
    for raw in re.findall(AMOUNT, text):
        if round(float(raw.replace(",", "")), 2) not in valid_amounts:
            problems.append(f"amount not backed by a tool: {raw}")
    if re.search(THREATS, t):
        problems.append("threatening language")
    if re.search(OUT_OF_POLICY, t) or any(
            re.search(FORBIDDEN_INSTALLMENTS, s) and not re.search(NEGATION, s)
            for s in re.split(r"[.!?\n]+", t)):
        problems.append("out-of-policy promise")
    return problems
