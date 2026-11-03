"""Orquestador: un turno de WhatsApp de punta a punta.

Flujo por mensaje entrante:
  1. Verificación de identidad (código, no LLM).
  2. Guardrail de entrada -> ¿escalamiento obligatorio?
  3. Loop de tool use: el modelo decide qué tool llamar hasta dar una respuesta final.
  4. Garantía de escalamiento: si una regla lo exigía y el modelo no escaló, escala el código.
  5. Guardrail de salida -> si el mensaje falla, se envía un mensaje seguro y se escala.
En producción esto es una Lambda / AgentCore Runtime disparada desde SQS FIFO."""
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

import anthropic

from . import config, guardrails, tools
from .store import Store, auditar


@dataclass
class Sesion:
    """Memoria de la conversación (en producción: un ítem en DynamoDB con TTL)."""
    cliente_id: str
    verificado: bool = False
    insultos: int = 0
    caso: dict | None = None  # se llena al escalar; a partir de ahí responde el humano
    mensajes: list[dict] = field(default_factory=list)
    montos_validos: set[float] = field(default_factory=set)

    def a_dict(self) -> dict:
        """JSON-serializable: en AWS la Lambda es stateless y la sesión viaja a
        DynamoDB entre turnos (un set no es JSON, por eso va como lista)."""
        return {**asdict(self), "montos_validos": sorted(self.montos_validos)}

    @classmethod
    def de_dict(cls, d: dict) -> "Sesion":
        return cls(**{**d, "montos_validos": set(d.get("montos_validos", []))})


@dataclass
class Turno:
    respuesta: str
    decision: str  # "GESTIONA" | "ESCALA"


class Agente:
    def __init__(self, llm, store: Store, clientes: dict[str, dict],
                 al_evento: Callable[[dict], None] = lambda e: None):
        self.llm, self.store, self.clientes, self.al_evento = llm, store, clientes, al_evento
        self.system = config.RUTA_PROMPT.read_text(encoding="utf-8")
        self.sesiones: dict[str, Sesion] = {}

    def responder(self, cliente_id: str, texto: str) -> Turno:
        s = self.sesiones.setdefault(cliente_id, Sesion(cliente_id))
        auditar("mensaje_entrante", cliente_id, texto=texto)
        plantilla = self._plantilla_respondida(s)  # antes de anotar el mensaje del cliente
        self.store.guardar("conversacion", {"cliente_id": cliente_id, "rol": "cliente", "texto": texto})
        if s.caso:  # ya hay un humano a cargo: el bot no vuelve a cobrar
            return self._cerrar(s, f"Tu caso {s.caso['caso_id']} ya está con un asesor; te escribirá pronto.")

        if plantilla:
            s.mensajes += self._apertura_por_plantilla(plantilla)
        self._verificar(s, texto)
        motivo = self._guardrail_entrada(s, texto)
        s.mensajes.append({"role": "user", "content": [
            {"type": "text", "text": self._nota_contexto(s, motivo, plantilla)}, {"type": "text", "text": texto}]})

        respuesta = self._loop(s)
        if respuesta is None:  # error de API, refusal o pasos agotados -> nunca dejar al cliente colgado
            motivo = motivo or "sin_resolucion"
        forzado = bool(motivo) and not s.caso
        if forzado:
            self._ejecutar(s, "escalar_a_humano", {"motivo": motivo, "resumen": f"Escalamiento forzado por "
                           f"el orquestador ({motivo}). Último mensaje: '{texto[:160]}'"})
        if forzado or respuesta is None:
            respuesta = tools.texto_escalamiento(s.caso)
        return self._cerrar(s, self._guardrail_salida(s, respuesta))

    # -- pasos del turno ---------------------------------------------------
    def _plantilla_respondida(self, s: Sesion) -> dict | None:
        """Si lo último que recibió el cliente fue una plantilla de campaña, le está
        respondiendo. La plantilla entra a la conversación del modelo como su
        mensaje anterior (ver _apertura_por_plantilla)."""
        conversacion = self.store.buscar("conversacion", s.cliente_id)
        ultimo = conversacion[-1] if conversacion else None
        if ultimo and ultimo["rol"] == "campana":
            self._evento(s, "campana", detalle=f"El cliente responde a la plantilla {ultimo['plantilla']} "
                                                f"({ultimo['campana_id']})")
            return ultimo
        return None

    @staticmethod
    def _apertura_por_plantilla(plantilla: dict) -> list[dict]:
        """El modelo debe 'recordar' que ya escribió primero. Hallazgo en la demo: con
        solo una nota de contexto, Claude volvía a saludar y presentarse, como si la
        conversación empezara de cero. Por eso la plantilla entra como SU mensaje
        anterior (rol assistant). La Messages API exige empezar con 'user', así que va
        precedida de un aviso del sistema que explica de dónde salió ese mensaje."""
        aviso = (f"[CONTEXTO DEL SISTEMA, no escrito por el cliente] LLA inició esta conversación por "
                 f"WhatsApp con la plantilla aprobada {plantilla['plantilla']} ({plantilla['campana_id']}). "
                 f"El siguiente mensaje del asistente es exactamente lo que recibió el cliente.")
        return [{"role": "user", "content": [{"type": "text", "text": aviso}]},
                {"role": "assistant", "content": [{"type": "text", "text": plantilla["texto"]}]}]

    def _verificar(self, s: Sesion, texto: str) -> None:
        """Verificación mínima en un paso: últimos 4 dígitos de la cédula. Es
        proporcional al riesgo (el número de WhatsApp ya está registrado en el CRM)."""
        ultimos4 = re.sub(r"\D", "", self.clientes[s.cliente_id]["cedula"])[-4:]
        if not s.verificado and ultimos4 in re.findall(r"\b\d{4}\b", texto):
            s.verificado = True
            self._evento(s, "verificacion", detalle="identidad verificada (últimos 4 dígitos de la cédula)")

    def _guardrail_entrada(self, s: Sesion, texto: str) -> str | None:
        motivo = guardrails.revisar_entrada(texto)
        if guardrails.es_insulto(texto):
            s.insultos += 1
            motivo = motivo or ("legal_o_insultos" if s.insultos >= 2 else None)  # 'persistentes' = 2+
        if motivo:
            self._evento(s, "guardrail", detalle=f"entrada -> escalamiento obligatorio: {motivo}")
        return motivo

    def _nota_contexto(self, s: Sesion, motivo: str | None, plantilla: dict | None = None) -> str:
        """Hechos que el modelo necesita y que vienen del sistema, no del cliente.
        Va en un bloque separado para que el texto del cliente no pueda suplantarlo
        (y aunque lo hiciera, las tools validan contra la sesión, no contra la nota)."""
        nota = (f"[CONTEXTO DEL SISTEMA, no escrito por el cliente] cliente_id={s.cliente_id}; "
                f"identidad_verificada={'si' if s.verificado else 'no'}; fecha_hoy={config.hoy()}")
        if plantilla:  # sin ';' ni ']' dentro del valor: así se parsea la nota
            nota += (f"; responde_a_plantilla={plantilla['plantilla']} enviada el {plantilla['creado'][:16]}, "
                     f"un aviso sin monto: el cliente aún no conoce el monto ni el motivo")
        return nota + (f"; escalamiento_obligatorio={motivo}]" if motivo else "]")

    def _loop(self, s: Sesion) -> str | None:
        """Loop agéntico manual (no el tool runner de la SDK) porque el mock
        offline debe pasar por exactamente el mismo camino que Claude."""
        for _ in range(config.POLITICA["max_pasos_por_turno"]):
            try:
                r = self.llm.crear(self.system, s.mensajes, tools.SCHEMAS)
            except anthropic.APIError as exc:
                self._evento(s, "error", detalle=f"falla del modelo: {type(exc).__name__}")
                return None
            s.mensajes.append({"role": "assistant", "content": r.contenido})
            if r.stop_reason == "refusal":
                self._evento(s, "error", detalle="el modelo declinó responder")
                return None
            usos = [b for b in r.contenido if b["type"] == "tool_use"]
            if not usos:
                return "".join(b["text"] for b in r.contenido if b["type"] == "text").strip()
            s.mensajes.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u["id"], **self._ejecutar(s, u["name"], u["input"])}
                for u in usos]})
        self._evento(s, "error", detalle="se agotaron los pasos del turno")
        return None

    def _ejecutar(self, s: Sesion, nombre: str, entrada: dict) -> dict:
        ctx = tools.Contexto(self.clientes[s.cliente_id], self.store, config.hoy(), s.verificado)
        resultado = tools.ejecutar(nombre, entrada, ctx)
        s.montos_validos |= guardrails.montos_en(resultado)
        if nombre == "escalar_a_humano" and resultado["ok"]:
            s.caso = resultado
        self._evento(s, "tool", nombre=nombre, entrada=entrada, resultado=resultado)
        return {"content": tools.resultado_a_texto(resultado), "is_error": not resultado["ok"]}

    def _guardrail_salida(self, s: Sesion, respuesta: str) -> str:
        problemas = guardrails.revisar_salida(respuesta, s.montos_validos)
        if not problemas:
            return respuesta
        self._evento(s, "guardrail", detalle=f"salida bloqueada: {'; '.join(problemas)}", original=respuesta)
        if not s.caso:  # el mensaje seguro promete un asesor: hay que cumplirlo
            self._ejecutar(s, "escalar_a_humano", {"motivo": "sin_resolucion",
                                                   "resumen": f"Respuesta bloqueada por guardrail: {problemas}"})
        return guardrails.MENSAJE_SEGURO

    def _cerrar(self, s: Sesion, respuesta: str) -> Turno:
        turno = Turno(respuesta, "ESCALA" if s.caso else "GESTIONA")
        self.store.guardar("conversacion", {"cliente_id": s.cliente_id, "rol": "agente", "texto": respuesta,
                                            "decision": turno.decision})
        auditar("mensaje_saliente", s.cliente_id, texto=respuesta, decision=turno.decision)
        return turno

    def _evento(self, s: Sesion, tipo: str, **datos) -> None:
        auditar(tipo, s.cliente_id, **datos)
        self.al_evento({"tipo": tipo, **datos})
