"""El 'cerebro' detrás de una interfaz mínima: crear(system, messages, tools).

Tres implementaciones con la MISMA interfaz (bloques de contenido estilo
Messages API: text / tool_use), así el agente, las tools y los guardrails no
saben ni les importa quién decide:
- ClaudeLLM(bedrock=False): Claude API con la SDK oficial de Anthropic.
- ClaudeLLM(bedrock=True):  Amazon Bedrock (la ruta de producción en AWS).
- MockLLM: motor de reglas determinístico para correr sin credenciales. NO es
  un LLM: imita sus decisiones para que la demo sea reproducible."""
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import anthropic

from . import config
from .tools import texto_escalamiento


@dataclass
class Respuesta:
    contenido: list[dict]
    stop_reason: str


class ClaudeLLM:
    def __init__(self, bedrock: bool = False):
        # Timeout por llamada: en AWS el HTTP API corta a los 30 s, así que conviene
        # fallar rápido y que el orquestador escale, en vez de dejar al cliente sin respuesta.
        # max_retries=3: la cuenta de demo tiene un límite de 10 requests/min en Bedrock;
        # la SDK reintenta con backoff exponencial los 429 (RateLimitError).
        opciones = {"timeout": config.TIMEOUT_LLM_S, "max_retries": 3}
        if bedrock:
            # bedrock-runtime InvokeModel firmado con IAM (el rol de la Lambda). Se usa
            # este cliente y no AnthropicBedrockMantle porque acepta los inference
            # profiles cross-region (us.anthropic.*) habilitados en la cuenta y se
            # autoriza con bedrock:InvokeModel, la acción IAM estándar.
            self.cliente = anthropic.AnthropicBedrock(aws_region=config.REGION_AWS, **opciones)
            self.modelo, self.etiqueta = config.MODELO_BEDROCK, f"Amazon Bedrock · {config.MODELO_BEDROCK}"
        else:
            self.cliente = anthropic.Anthropic(**opciones)
            self.modelo, self.etiqueta = config.MODELO, f"Claude API · {config.MODELO}"
        self.extra = {"output_config": {"effort": config.ESFUERZO}}

    def crear(self, system: str, messages: list[dict], tools: list[dict]) -> Respuesta:
        try:
            r = self.cliente.messages.create(model=self.modelo, max_tokens=8000, system=system,
                                             tools=tools, messages=messages, **self.extra)
        except anthropic.BadRequestError as exc:
            if not self.extra or ("output_config" not in str(exc) and "effort" not in str(exc)):
                raise
            # Degradación elegante: si el endpoint/modelo no acepta `effort`, se
            # sigue con el esfuerzo por defecto del modelo en vez de escalar todo.
            self.extra = {}
            return self.crear(system, messages, tools)
        # Se devuelve el contenido completo (incluye bloques de thinking) para
        # reenviarlo intacto en el siguiente request, como exige la API.
        return Respuesta([b.model_dump(exclude_none=True) for b in r.content], r.stop_reason)


def crear_llm():
    """Elige el cerebro según config.proveedor() (LLM_PROVIDER / ANTHROPIC_API_KEY)."""
    p = config.proveedor()
    return MockLLM() if p == "mock" else ClaudeLLM(bedrock=(p == "bedrock"))


# ---------------------------------------------------------------- modo offline
@dataclass
class _Llamada:
    nombre: str
    entrada: dict
    resultado: dict


DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _monto(t: str) -> float | None:
    m = re.search(r"(?:\$|usd|b/\.)\s?(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:dolares|balboas)", t)
    return float(m.group(1) or m.group(2)) if m else None


def _fecha(t: str, hoy: date) -> date | None:
    if m := re.search(r"en (\d+) dias", t):
        return hoy + timedelta(days=int(m.group(1)))
    if m := re.search(r"\d{4}-\d{2}-\d{2}", t):
        return date.fromisoformat(m.group(0))
    if "manana" in t:
        return hoy + timedelta(days=1)
    for i, dia in enumerate(DIAS):
        if f"el {dia}" in t:
            return hoy + timedelta(days=(i - hoy.weekday()) % 7 or 7)
    return None


class MockLLM:
    etiqueta = "OFFLINE · motor de reglas determinístico (sin LLM, sin red)"

    def crear(self, system: str, messages: list[dict], tools: list[dict]) -> Respuesta:
        decision = self._decidir(*self._leer(messages))
        if isinstance(decision, str):
            return Respuesta([{"type": "text", "text": decision}], "end_turn")
        nombre, entrada = decision
        bloque = {"type": "tool_use", "id": f"toolu_mock_{uuid.uuid4().hex[:10]}",
                  "name": nombre, "input": entrada}
        return Respuesta([bloque], "tool_use")

    @staticmethod
    def _leer(messages: list[dict]):
        """Reconstruye lo que un LLM 'vería': último texto del cliente, nota de
        contexto del sistema, y todas las tools ya ejecutadas (con su resultado)."""
        from .guardrails import normalizar
        usos, llamadas, inicio_turno, texto, contexto = {}, [], 0, "", {}
        for msg in messages:
            for b in msg["content"]:
                if b["type"] == "tool_use":
                    usos[b["id"]] = b
                elif b["type"] == "tool_result":
                    u = usos[b["tool_use_id"]]
                    llamadas.append(_Llamada(u["name"], u["input"], json.loads(b["content"])))
            textos = [b["text"] for b in msg["content"] if b["type"] == "text"]
            if msg["role"] == "user" and textos:
                contexto = dict(re.findall(r"(\w+)=([^;\]]+)", textos[0]))
                texto, inicio_turno = normalizar(textos[-1]), len(llamadas)
        return texto, contexto, llamadas, llamadas[inicio_turno:]

    def _decidir(self, texto, ctx, llamadas, del_turno):
        cid = ctx["cliente_id"]
        ultima = del_turno[-1] if del_turno else None
        if ultima and ultima.nombre == "escalar_a_humano" and ultima.resultado["ok"]:
            return texto_escalamiento(ultima.resultado)
        if motivo := ctx.get("escalamiento_obligatorio"):
            return self._escalar(cid, motivo, texto)
        if ctx.get("identidad_verificada") != "si":
            return ("Hola, soy el asistente virtual de LLA. Para proteger tus datos, "
                    "¿me confirmas los últimos 4 dígitos de tu cédula?")
        hechos = {c.nombre: c.resultado for c in llamadas if c.resultado["ok"]}
        for lectura in ("consultar_saldo", "consultar_historial"):
            if lectura not in hechos:
                return lectura, {"cliente_id": cid}
        cuenta = hechos["consultar_saldo"]
        if ultima and ultima.nombre not in ("consultar_saldo", "consultar_historial"):
            return self._reaccionar(cid, ultima, llamadas, cuenta, texto)
        if cuenta["segmento"] in ("en_disputa", "alto_valor_b2b"):
            motivo = "disputa" if cuenta["segmento"] == "en_disputa" else "alto_valor_b2b"
            return self._escalar(cid, motivo, texto)
        return self._intencion(cid, texto, cuenta, date.fromisoformat(ctx["fecha_hoy"]))

    @staticmethod
    def _escalar(cid, motivo, texto):
        return "escalar_a_humano", {"cliente_id": cid, "motivo": motivo,
                                    "resumen": f"Motivo: {motivo}. Último mensaje del cliente: '{texto[:160]}'"}

    def _intencion(self, cid, texto, cuenta, hoy):
        """Mapea lo que pide el cliente a UNA tool. La tool decide si se puede."""
        saldo = cuenta["saldo_vencido"]
        if m := re.search(r"(\d+)\s*cuotas", texto):
            return "ofrecer_acuerdo_pago", {"cliente_id": cid, "cuotas": int(m.group(1))}
        if re.search(r"acuerdo|en partes|cuotas", texto):
            return "ofrecer_acuerdo_pago", {"cliente_id": cid, "cuotas": 3}
        if fecha := _fecha(texto, hoy):
            return "registrar_promesa_pago", {"cliente_id": cid, "monto": _monto(texto) or saldo,
                                              "fecha": str(fecha)}
        if re.search(r"pagar|pago|link|enlace", texto):
            return "generar_link_pago", {"cliente_id": cid, "monto": _monto(texto) or saldo}
        if re.search(r"gracias|listo|perfecto", texto):
            return "¡Gracias a ti! Si necesitas algo más, escríbeme por aquí. Que tengas buen día."
        opciones = {"generar_link_pago": "pagar ahora con un link seguro",
                    "registrar_promesa_pago": "acordar una fecha de pago (hasta 15 días)",
                    "ofrecer_acuerdo_pago": "dividirlo en 2 o 3 cuotas"}
        lista = "; ".join(opciones[a] for a in cuenta["acciones_permitidas"] if a in opciones)
        return (f"Gracias, {cuenta['nombre']}. Tienes un saldo vencido de USD {saldo:.2f} "
                f"con {cuenta['dias_mora']} días de atraso. Puedo ayudarte a: {lista}. ¿Qué prefieres?")

    def _reaccionar(self, cid, ultima, llamadas, cuenta, texto):
        """Respuesta al resultado de la tool que se acaba de ejecutar."""
        r = ultima.resultado
        if r["ok"]:
            if ultima.nombre == "generar_link_pago":
                return (f"Listo. Aquí tienes tu link para pagar USD {r['monto']:.2f}: {r['url']} "
                        f"(válido hasta {r['vence']}). Tu servicio se normaliza al confirmarse el pago.")
            if ultima.nombre == "registrar_promesa_pago":
                return (f"Perfecto, registré tu compromiso de pagar USD {r['monto']:.2f} el {r['fecha']}. "
                        f"Te enviaré un recordatorio ese día. ¡Gracias por tu disposición!")
            cuotas = ", ".join(f"USD {c['monto']:.2f} el {c['fecha']}" for c in r["plan"])
            return f"Listo, tu acuerdo quedó registrado en {len(r['plan'])} cuotas: {cuotas}."
        codigo = r["error"]["codigo"]
        rechazos = sum(not c.resultado["ok"] and c.resultado["error"]["codigo"] in
                       ("CUOTAS_NO_PERMITIDAS", "ACUERDO_NO_DISPONIBLE", "ACCION_NO_PERMITIDA_SEGMENTO")
                       for c in llamadas)
        if codigo in ("CLIENTE_EN_DISPUTA", "REQUIERE_EJECUTIVO"):
            return self._escalar(cid, "disputa" if codigo == "CLIENTE_EN_DISPUTA" else "alto_valor_b2b", texto)
        if rechazos >= 2:  # el cliente insiste en algo que la política no permite
            return self._escalar(cid, "politica_no_permite", texto)
        if codigo == "FECHA_FUERA_DE_POLITICA":
            e = r["error"]
            return (f"Entiendo. Puedo registrar tu compromiso con fecha máxima {e['fecha_maxima']}: "
                    f"por el total (USD {e['saldo_vencido']:.2f}) o un abono desde "
                    f"USD {e['monto_minimo_parcial']:.2f}. ¿Qué monto y fecha te funcionan?")
        if codigo == "MONTO_FUERA_DE_POLITICA":
            return (f"Para un abono parcial el mínimo es USD {r['error']['monto_minimo_parcial']:.2f}. "
                    f"¿Te funciona ese monto?")
        if codigo in ("ACUERDO_NO_DISPONIBLE", "ACCION_NO_PERMITIDA_SEGMENTO"):
            return (f"{cuenta['nombre']}, en tu caso no puedo ofrecer un acuerdo en cuotas. Lo que sí "
                    f"puedo hacer hoy es enviarte un link para pagar el total de USD "
                    f"{cuenta['saldo_vencido']:.2f} y reactivar tu servicio. ¿Te lo envío?")
        if codigo == "CUOTAS_NO_PERMITIDAS":
            return "Puedo ofrecerte el saldo en 2 o 3 cuotas. ¿Cuál prefieres?"
        if codigo == "PROMESA_ACTIVA_EXISTENTE":
            return "Ya tienes un compromiso de pago activo; te recordaré la fecha acordada."
        return self._escalar(cid, "sin_resolucion", texto)  # error inesperado: mejor un humano
