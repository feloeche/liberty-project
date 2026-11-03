"""Herramientas del agente (mismos nombres que en la propuesta de arquitectura, §5.3).

Decisión central: las políticas (montos, plazos, cuotas, quién puede negociar)
se validan AQUÍ, en código determinístico. El LLM solo propone; si propone algo
fuera de política recibe un error estructurado con alternativas y debe
reconducir la conversación. Así un prompt injection o una alucinación no puede
"regalar" 6 cuotas o una fecha a 60 días."""
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from .config import POLITICA
from .store import Store

MOTIVOS = ["disputa", "vulnerabilidad", "solicita_humano", "legal_o_insultos",
           "alto_valor_b2b", "politica_no_permite", "sin_resolucion"]

# Qué puede ofrecer el agente según segmento (propuesta de arquitectura, §1.2).
ACCIONES = {
    "olvidadizo": ["generar_link_pago", "registrar_promesa_pago"],
    "dificultad_temporal": ["generar_link_pago", "registrar_promesa_pago", "ofrecer_acuerdo_pago"],
    "reincidente": ["generar_link_pago"],
    "en_disputa": ["escalar_a_humano"],
    "alto_valor_b2b": ["escalar_a_humano"],
}

COLAS = {"disputa": "reclamos", "alto_valor_b2b": "ejecutivo_b2b", "vulnerabilidad": "cuidado_especial"}

_ID = {"type": "string", "description": "ID interno del cliente, p.ej. C001"}
SCHEMAS = [
    {"name": "consultar_saldo",
     "description": "Saldo vencido, días de mora, facturas, servicios, estado y segmento del cliente. "
                    "Requiere identidad verificada.",
     "input_schema": {"type": "object", "properties": {"cliente_id": _ID}, "required": ["cliente_id"]}},
    {"name": "consultar_historial",
     "description": "Promesas previas (cumplidas/incumplidas), contactos recientes y reclamos abiertos.",
     "input_schema": {"type": "object", "properties": {"cliente_id": _ID}, "required": ["cliente_id"]}},
    {"name": "generar_link_pago",
     "description": "Genera un link de pago por un monto en USD (mayor a 0 y menor o igual al saldo).",
     "input_schema": {"type": "object", "properties": {"cliente_id": _ID, "monto": {"type": "number"}},
                      "required": ["cliente_id", "monto"]}},
    {"name": "registrar_promesa_pago",
     "description": "Registra una promesa de pago. Políticas: fecha como máximo a 15 días, "
                    "si es parcial al menos 30% del saldo, máximo 1 promesa activa.",
     "input_schema": {"type": "object", "properties": {
         "cliente_id": _ID, "monto": {"type": "number"},
         "fecha": {"type": "string", "description": "Fecha AAAA-MM-DD"}},
         "required": ["cliente_id", "monto", "fecha"]}},
    {"name": "ofrecer_acuerdo_pago",
     "description": "Registra un acuerdo en cuotas (solo 2 o 3) si la campaña y el segmento lo permiten.",
     "input_schema": {"type": "object", "properties": {"cliente_id": _ID, "cuotas": {"type": "integer"}},
                      "required": ["cliente_id", "cuotas"]}},
    {"name": "escalar_a_humano",
     "description": "Crea un caso en la cola humana con un resumen de la conversación. "
                    "Úsala siempre que aplique una regla de escalamiento obligatorio.",
     "input_schema": {"type": "object", "properties": {
         "cliente_id": _ID, "motivo": {"type": "string", "enum": MOTIVOS},
         "resumen": {"type": "string"}}, "required": ["cliente_id", "motivo", "resumen"]}},
]


@dataclass
class Contexto:
    """Lo que la tool sabe además de sus argumentos. 'verificado' viene de la
    sesión (código), nunca de lo que diga el LLM."""
    cliente: dict
    store: Store
    hoy: date
    verificado: bool


def ok(**datos) -> dict:
    return {"ok": True, **datos}


def error(codigo: str, mensaje: str, **extra) -> dict:
    return {"ok": False, "error": {"codigo": codigo, "mensaje": mensaje, **extra}}


def saldo_de(cliente: dict) -> float:
    return round(sum(f["monto"] for f in cliente["facturas_vencidas"]), 2)


def segmentar(cliente: dict) -> str:
    """Reglas simples y explicables, en orden de prioridad. Fase 2: scoring de
    propensión de pago (SageMaker) en vez de umbrales fijos."""
    incumplidas = sum(p["estado"] == "incumplida" for p in cliente["historial"]["promesas"])
    if cliente["tipo"] == "B2B" or saldo_de(cliente) > POLITICA["umbral_humano_usd"]:
        return "alto_valor_b2b"
    if cliente["historial"]["reclamos_abiertos"]:
        return "en_disputa"
    if incumplidas >= 2:
        return "reincidente"
    if cliente["dias_mora"] <= 30 and incumplidas == 0:
        return "olvidadizo"
    return "dificultad_temporal"


def _tramo(dias: int) -> str:
    return "1-30" if dias <= 30 else "31-60" if dias <= 60 else "61-90" if dias <= 90 else "90+"


def _bloqueo_cobro(ctx: Contexto, accion: str) -> dict | None:
    """Barreras comunes a toda tool que cobra o negocia."""
    if not ctx.verificado:
        return error("IDENTIDAD_NO_VERIFICADA", "Pide los últimos 4 dígitos de la cédula antes de continuar.")
    segmento = segmentar(ctx.cliente)
    if segmento == "en_disputa":
        return error("CLIENTE_EN_DISPUTA", "Hay un reclamo abierto: no se cobra, se escala.",
                     alternativas=["escalar_a_humano"])
    if segmento == "alto_valor_b2b":
        return error("REQUIERE_EJECUTIVO", "Cliente B2B o monto alto: lo gestiona un ejecutivo.",
                     alternativas=["escalar_a_humano"])
    if accion not in ACCIONES[segmento]:
        return error("ACCION_NO_PERMITIDA_SEGMENTO",
                     f"El segmento '{segmento}' no permite {accion}.", alternativas=ACCIONES[segmento])
    return None


def consultar_saldo(ctx: Contexto) -> dict:
    """Minimización de datos (Ley 81 de 2019): no devuelve cédula ni teléfono."""
    if not ctx.verificado:
        return error("IDENTIDAD_NO_VERIFICADA", "Pide los últimos 4 dígitos de la cédula antes de continuar.")
    c = ctx.cliente
    segmento = segmentar(c)
    return ok(nombre=c["nombre"].split()[0], saldo_vencido=saldo_de(c), dias_mora=c["dias_mora"],
              tramo_mora=_tramo(c["dias_mora"]), facturas=c["facturas_vencidas"],
              servicios=c["servicios"], estado_servicio=c["estado_servicio"], tipo=c["tipo"],
              segmento=segmento, acciones_permitidas=ACCIONES[segmento],
              limites_politica=_limites(ctx, segmento))


def _limites(ctx: Contexto, segmento: str) -> dict:
    """Los números de la política ya calculados por el código. Hallazgo con el LLM
    real: Claude calculaba solo el 30% (USD 42.83) y el guardrail de montos lo
    bloqueaba con razón, porque ese número no venía de una tool. Si el código
    entrega el cálculo, el modelo no hace aritmética y todo monto queda respaldado."""
    saldo, limites = saldo_de(ctx.cliente), {}
    if "registrar_promesa_pago" in ACCIONES[segmento]:  # solo lo que el segmento puede usar
        limites["monto_minimo_parcial"] = math.ceil(saldo * POLITICA["pct_min_parcial"] * 100) / 100
        limites["fecha_maxima_promesa"] = str(ctx.hoy + timedelta(days=POLITICA["max_dias_promesa"]))
    if "ofrecer_acuerdo_pago" in ACCIONES[segmento] and ctx.cliente["campana_permite_acuerdo"]:
        limites["cuotas_permitidas"] = {n: math.floor(saldo / n * 100) / 100
                                        for n in POLITICA["cuotas_permitidas"]}
    return limites


def consultar_historial(ctx: Contexto) -> dict:
    if not ctx.verificado:
        return error("IDENTIDAD_NO_VERIFICADA", "Pide los últimos 4 dígitos de la cédula antes de continuar.")
    h = ctx.cliente["historial"]
    nuevas = ctx.store.buscar("promesas", ctx.cliente["cliente_id"])
    return ok(promesas=h["promesas"] + nuevas, contactos_recientes=h["contactos_recientes"],
              reclamos_abiertos=h["reclamos_abiertos"])


def generar_link_pago(ctx: Contexto, monto: float) -> dict:
    if bloqueo := _bloqueo_cobro(ctx, "generar_link_pago"):
        return bloqueo
    saldo = saldo_de(ctx.cliente)
    if not 0 < monto <= saldo:
        return error("MONTO_INVALIDO", "El monto debe ser mayor a 0 y no superar el saldo.", saldo_vencido=saldo)
    link = ctx.store.guardar("links", {
        "cliente_id": ctx.cliente["cliente_id"], "monto": round(monto, 2),
        "url": f"https://pagos.lla.example/pa/{uuid.uuid4().hex[:8]}",
        "vence": str(ctx.hoy + timedelta(days=2))})
    return ok(**link)


def registrar_promesa_pago(ctx: Contexto, monto: float, fecha: str) -> dict:
    if bloqueo := _bloqueo_cobro(ctx, "registrar_promesa_pago"):
        return bloqueo
    cid, saldo = ctx.cliente["cliente_id"], saldo_de(ctx.cliente)
    minimo = math.ceil(saldo * POLITICA["pct_min_parcial"] * 100) / 100
    maxima = ctx.hoy + timedelta(days=POLITICA["max_dias_promesa"])
    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        return error("FECHA_INVALIDA", "Usa el formato AAAA-MM-DD.")
    if not ctx.hoy < dia <= maxima:
        return error("FECHA_FUERA_DE_POLITICA", f"La fecha debe estar entre mañana y {maxima}.",
                     fecha_maxima=str(maxima), saldo_vencido=saldo, monto_minimo_parcial=minimo)
    if not minimo <= monto <= saldo:
        return error("MONTO_FUERA_DE_POLITICA", "Un pago parcial debe ser al menos el 30% del saldo.",
                     monto_minimo_parcial=minimo, saldo_vencido=saldo)
    if any(p["estado"] == "activa" for p in ctx.store.buscar("promesas", cid)):
        return error("PROMESA_ACTIVA_EXISTENTE", "Ya existe una promesa activa para este cliente.")
    promesa = ctx.store.guardar("promesas", {"cliente_id": cid, "monto": round(monto, 2),
                                             "fecha": str(dia), "estado": "activa"})
    return ok(**promesa, saldo_restante=round(saldo - monto, 2))


def ofrecer_acuerdo_pago(ctx: Contexto, cuotas: int) -> dict:
    if bloqueo := _bloqueo_cobro(ctx, "ofrecer_acuerdo_pago"):
        return bloqueo
    cid, saldo = ctx.cliente["cliente_id"], saldo_de(ctx.cliente)
    if not ctx.cliente["campana_permite_acuerdo"]:
        return error("ACUERDO_NO_DISPONIBLE", "La campaña actual no permite acuerdos.",
                     alternativas=["generar_link_pago", "registrar_promesa_pago"])
    if cuotas not in POLITICA["cuotas_permitidas"]:
        return error("CUOTAS_NO_PERMITIDAS", "Solo se permiten 2 o 3 cuotas.",
                     cuotas_permitidas=list(POLITICA["cuotas_permitidas"]))
    if ctx.store.buscar("acuerdos", cid):
        return error("ACUERDO_EXISTENTE", "El cliente ya tiene un acuerdo registrado.")
    base = math.floor(saldo / cuotas * 100) / 100  # la última cuota absorbe los centavos
    plan = [{"cuota": i + 1, "fecha": str(ctx.hoy + timedelta(days=30 * i + 7)),
             "monto": base if i < cuotas - 1 else round(saldo - base * (cuotas - 1), 2)}
            for i in range(cuotas)]
    return ok(**ctx.store.guardar("acuerdos", {"cliente_id": cid, "total": saldo, "plan": plan}))


def escalar_a_humano(ctx: Contexto, motivo: str, resumen: str) -> dict:
    """Siempre disponible (incluso sin verificar identidad): escalar nunca debe fallar."""
    if motivo not in MOTIVOS:
        motivo = "sin_resolucion"
    caso = ctx.store.guardar("escalamientos", {
        "cliente_id": ctx.cliente["cliente_id"], "caso_id": f"CASO-{ctx.store.contar('escalamientos') + 1:04d}",
        "motivo": motivo, "cola": COLAS.get(motivo, "cobranza_humana"),
        "sla_horas": 2 if motivo == "vulnerabilidad" else 24,
        # Minimización de datos (Ley 81/2019): el asesor no necesita los dígitos de la cédula.
        "resumen": re.sub(r"\d{4,}", "****", resumen)})
    return ok(**caso)


_TOOLS = {f.__name__: f for f in (consultar_saldo, consultar_historial, generar_link_pago,
                                    registrar_promesa_pago, ofrecer_acuerdo_pago, escalar_a_humano)}


def ejecutar(nombre: str, args: dict, ctx: Contexto) -> dict:
    """Punto único de ejecución: ignora el cliente_id que mande el LLM y usa el
    de la sesión, para que el modelo no pueda consultar a otro cliente."""
    funcion = _TOOLS.get(nombre)
    if funcion is None:
        return error("TOOL_DESCONOCIDA", f"No existe la herramienta {nombre}.")
    args = {k: v for k, v in args.items() if k != "cliente_id"}
    try:
        return funcion(ctx, **args)
    except (TypeError, ValueError) as exc:
        return error("ARGUMENTOS_INVALIDOS", str(exc))


def texto_escalamiento(caso: dict) -> str:
    """Mensaje de handoff: honesto (dice que pasa a una persona) y con número de caso."""
    inicio = {
        "disputa": "Entiendo tu inquietud con ese cobro. No te pediremos ese pago mientras se revisa.",
        "vulnerabilidad": "Lamento mucho lo que estás pasando. Tu tranquilidad es lo primero.",
        "solicita_humano": "Con gusto te comunico con una persona de nuestro equipo.",
        "alto_valor_b2b": "Por el tipo de cuenta, tu caso lo atiende un ejecutivo dedicado.",
        "politica_no_permite": "Entiendo tu situación; esa opción no la puedo autorizar yo.",
    }.get(caso["motivo"], "Quiero asegurarme de darte la mejor solución.")
    return (f"{inicio} He pasado tu caso a un asesor de LLA (caso {caso['caso_id']}), "
            f"quien te contactará en menos de {caso['sla_horas']} horas por este mismo chat.")


def resultado_a_texto(resultado: dict) -> str:
    return json.dumps(resultado, ensure_ascii=False)
