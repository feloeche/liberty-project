"""Contacto proactivo: campañas de aviso por WhatsApp (propuesta, §4.2).

Quién recibe un mensaje lo deciden reglas DETERMINÍSTICAS en código, no el LLM:
son obligaciones de cumplimiento (consentimiento, horario, no cobrar en disputa)
y tienen que ser auditables y repetibles. El LLM entra recién cuando el cliente
responde, y por el mismo camino de siempre (verificación, tools, guardrails).

La plantilla (categoría UTILITY, aprobada por Meta para escribir fuera de la
ventana de 24 h) NO lleva monto a propósito: si el número fue reasignado, el
mensaje lo lee un tercero y revelaría una deuda. El monto solo se muestra
después de verificar la identidad. En producción la lista de clientes saldría
de una consulta de Athena sobre el data lake, no del JSON de demo."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import config
from .store import auditar
from .tools import ACCIONES, saldo_de, segmentar

PANAMA = ZoneInfo("America/Panama")
HORARIO = (8, 19)  # 8:00 a 18:59, lunes a sábado: horario permitido para contactar
DIAS_ENTRE_CONTACTOS = 3
SISTEMA = "SISTEMA"  # partition key de los ítems que no son de un cliente (las campañas)
PLANTILLA = {"nombre": "aviso_cuenta_v1", "categoria": "UTILITY",
             "texto": "Hola {{1}}, te escribe el asistente virtual de LLA. Tienes un aviso pendiente sobre "
                      "tu cuenta. Responde a este mensaje para revisarlo. Para no recibir estos avisos, "
                      "responde BAJA."}
NOMBRES = {"olvidadizo": "Olvidadizo", "dificultad_temporal": "Dificultad temporal",
           "reincidente": "Reincidente", "en_disputa": "En disputa", "alto_valor_b2b": "Alto valor B2B"}


def ahora_panama() -> datetime:
    """Fecha de negocio (AGIL_HOY, para demos reproducibles) con la hora real de Panamá."""
    return datetime.combine(config.hoy(), datetime.now(PANAMA).time(), PANAMA)


def en_horario(ahora: datetime) -> bool:
    return ahora.weekday() < 6 and HORARIO[0] <= ahora.hour < HORARIO[1]


def ultimo_contacto(cliente: dict, store) -> str | None:
    """El más reciente entre los contactos del CRM y los que registró una campaña."""
    fechas = [c["fecha"] for c in cliente["historial"]["contactos_recientes"]]
    # 'fecha' es la fecha de negocio de la campaña (no el reloj del servidor), igual que la del CRM.
    fechas += [c.get("fecha", c["creado"]) for c in store.buscar("contactos", cliente["cliente_id"])]
    return max(fechas, default=None)  # ISO: el orden de texto es el orden temporal


def motivo_supresion(cliente: dict, store, ahora: datetime, forzar_horario: bool) -> tuple[str, str] | None:
    """(motivo, detalle) de la PRIMERA regla que impide contactar, o None si se puede.
    El orden importa: primero el consentimiento y la ley, después la operación."""
    cid, h = cliente["cliente_id"], cliente["historial"]
    reclamo = h["reclamos_abiertos"][0]["ticket"] if h["reclamos_abiertos"] else None
    casos = store.buscar("escalamientos", cid)
    ultimo = ultimo_contacto(cliente, store) or ""  # "" y no None: los detalles se arman todos de una vez
    dias = (ahora.date() - date.fromisoformat(ultimo[:10])).days if ultimo else None
    reglas = [
        ("sin_opt_in", not cliente.get("opt_in"), "No dio consentimiento para recibir mensajes por WhatsApp"),
        ("opt_out", cliente.get("opt_out"), "Pidió no recibir más avisos (BAJA)"),
        ("fuera_de_horario", not forzar_horario and not en_horario(ahora),
         f"Fuera del horario permitido (lun-sáb {HORARIO[0]}:00-{HORARIO[1]}:00, hora de Panamá)"),
        ("reclamo_abierto", reclamo, f"Reclamo {reclamo} abierto: no se cobra durante una disputa"),
        ("segmento_no_cobrable", segmentar(cliente) == "alto_valor_b2b", "Cuenta B2B: la gestiona un ejecutivo"),
        ("caso_humano_abierto", casos, f"Caso {casos[-1]['caso_id']} en manos de un asesor" if casos else ""),
        ("promesa_vigente", any(p["estado"] == "activa" for p in store.buscar("promesas", cid)),
         "Tiene una promesa de pago vigente"),
        ("contactado_recientemente", dias is not None and dias < DIAS_ENTRE_CONTACTOS,
         f"Contactado el {ultimo[:10]} (mínimo {DIAS_ENTRE_CONTACTOS} días entre contactos)"),
        ("sin_saldo", saldo_de(cliente) <= 0, "No tiene saldo vencido"),
    ]
    return next(((motivo, detalle) for motivo, aplica, detalle in reglas if aplica), None)


def listar_segmentos(clientes: dict) -> list[dict]:
    return [{"id": s, "nombre": NOMBRES[s],
             "elegibles_estimados": sum(segmentar(c) == s for c in clientes.values())} for s in ACCIONES]


def ejecutar_campana(clientes: dict, store, segmentos: list[str] | None = None,
                     ahora: datetime | None = None, forzar_horario: bool = False) -> dict:
    """Evalúa las reglas para cada cliente de los segmentos pedidos (todos si no
    se indica) y "envía" la plantilla a los elegibles: registra el contacto (para
    la regla de 3 días), el mensaje en la conversación visible y la campaña."""
    ahora = ahora or ahora_panama()
    pedidos = segmentos or list(ACCIONES)
    campana_id = f"CMP-{ahora:%Y%m%d}-{store.contar('campanas') + 1:04d}"
    resultados = []
    for cid, c in clientes.items():
        if (segmento := segmentar(c)) not in pedidos:
            continue
        fila = {"cliente_id": cid, "nombre": c["nombre"], "segmento": segmento}
        if supresion := motivo_supresion(c, store, ahora, forzar_horario):
            resultados.append({**fila, "estado": "suprimido", "motivo": supresion[0], "detalle": supresion[1]})
            continue
        texto = PLANTILLA["texto"].replace("{{1}}", c["nombre"].split()[0])
        store.guardar("contactos", {"cliente_id": cid, "campana_id": campana_id, "canal": "whatsapp",
                                    "fecha": ahora.isoformat(timespec="minutes"),
                                    "plantilla": PLANTILLA["nombre"]})
        store.guardar("conversacion", {"cliente_id": cid, "rol": "campana", "texto": texto,
                                       "plantilla": PLANTILLA["nombre"], "campana_id": campana_id})
        auditar("plantilla_enviada", cid, campana_id=campana_id, plantilla=PLANTILLA["nombre"], texto=texto)
        resultados.append({**fila, "estado": "enviado", "texto": texto})
    por_motivo: dict[str, int] = {}
    for r in resultados:
        if r["estado"] == "suprimido":
            por_motivo[r["motivo"]] = por_motivo.get(r["motivo"], 0) + 1
    resumen = {"enviados": sum(r["estado"] == "enviado" for r in resultados),
               "suprimidos": sum(por_motivo.values()), "por_motivo": por_motivo}
    campana = {"campana_id": campana_id, "ejecutada": ahora.replace(tzinfo=None).isoformat(timespec="seconds"),
               "plantilla": PLANTILLA["nombre"], "segmentos": pedidos, "forzar_horario": forzar_horario,
               "resumen": resumen}
    store.guardar("campanas", {"cliente_id": SISTEMA, **campana})
    auditar("campana", SISTEMA, **campana)  # forzar_horario queda en la auditoría
    return {**campana, "resultados": resultados}
