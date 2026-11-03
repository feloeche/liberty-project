"""Guardrails determinísticos, antes y después del LLM.

Son a propósito reglas simples (palabras clave / regex): se pueden leer, auditar
y explicar a Legal y a Cumplimiento. No reemplazan a Bedrock Guardrails en
producción; son la primera capa barata y predecible. Ante la duda, escalan:
un falso positivo cuesta un minuto de un asesor, un falso negativo cuesta un
cliente vulnerable presionado o un reclamo ante ACODECO."""
import re
import unicodedata

# Entrada: si alguna regla se activa, el escalamiento es OBLIGATORIO.
REGLAS_ENTRADA = {
    "disputa": r"no reconozco|no lo reconozco|cobro indebido|cobro injusto|ya pague|no debo|"
               r"error en (la|mi) factura|reclamo|me estan cobrando de mas|no es mio",
    "vulnerabilidad": r"hospital|enferm|cancer|quimio|fallec|murio|luto|funeral|duelo|"
                      r"perdi (mi|el) (trabajo|empleo)|desemplead|no tengo (para|que) comer|"
                      r"deprimid|no aguanto mas|quiero morir",
    "solicita_humano": r"hablar con (una persona|un humano|alguien|un asesor|un agente)|"
                       r"\b(asesor|humano|persona real|operador)\b",
    "legal_o_insultos": r"acodeco|abogado|demanda|denuncia|tribunal|defensoria",
}
INSULTOS = r"\b(idiota|estupid|imbecil|ladron|estafador|maldit|basura|mierda)"

# Salida: lo que el agente nunca debe decir.
AMENAZAS = r"embarg|carcel|policia|demandar(emos|te)|abogados|te vamos a reportar|consecuencias legales"
FUERA_DE_POLITICA = r"descuento|condon|perdonar (la|tu) deuda|exoner|sin recargo"
CUOTAS_PROHIBIDAS = r"\b([4-9]|1\d)\s*cuotas"
# Hallazgo con el LLM real: "sobre las 6 cuotas, no están disponibles" es una
# NEGATIVA correcta y la regla anterior la bloqueaba. Se evalúa por oración.
NEGACION = r"\b(no|ni|solo|unicamente|lamentablemente|lamento|desafortunadamente)\b"
MONTO = r"(?:US\$|USD|B/\.|\$)\s?(\d[\d,]*(?:\.\d{1,2})?)"

MENSAJE_SEGURO = ("Gracias por tu mensaje. Para darte información precisa, un asesor de LLA "
                  "revisará tu caso y te escribirá por este chat.")


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes: 'Falleció' y 'fallecio' deben activar la misma regla."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sin_tildes.lower()


def revisar_entrada(texto: str) -> str | None:
    """Devuelve el motivo de escalamiento obligatorio, o None."""
    t = normalizar(texto)
    return next((motivo for motivo, patron in REGLAS_ENTRADA.items() if re.search(patron, t)), None)


def es_insulto(texto: str) -> bool:
    return bool(re.search(INSULTOS, normalizar(texto)))


def montos_en(obj) -> set[float]:
    """Todos los números que devolvieron las tools: es la 'lista blanca' de montos."""
    if isinstance(obj, bool):
        return set()
    if isinstance(obj, (int, float)):
        return {round(float(obj), 2)}
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        return set().union(*(montos_en(x) for x in obj)) if obj else set()
    return set()


def revisar_salida(texto: str, montos_validos: set[float]) -> list[str]:
    """Lista de problemas del mensaje saliente (vacía = se puede enviar).
    La regla de montos ataca la peor alucinación posible en cobranza: un monto inventado."""
    t = normalizar(texto)
    problemas = []
    for crudo in re.findall(MONTO, texto):
        if round(float(crudo.replace(",", "")), 2) not in montos_validos:
            problemas.append(f"monto no respaldado por una tool: {crudo}")
    if re.search(AMENAZAS, t):
        problemas.append("lenguaje de amenaza")
    if re.search(FUERA_DE_POLITICA, t) or any(
            re.search(CUOTAS_PROHIBIDAS, o) and not re.search(NEGACION, o)
            for o in re.split(r"[.!?\n]+", t)):
        problemas.append("promesa fuera de política")
    return problemas
