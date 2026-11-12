"""Proactive contact: WhatsApp notice campaigns (proposal, §4.2).

Who receives a message is decided by DETERMINISTIC rules in code, not by the LLM:
they are compliance obligations (consent, contact hours, no collecting during a
dispute) and must be auditable and repeatable. The LLM only comes in when the
customer replies, through the usual path (verification, tools, guardrails).

The template (UTILITY category, approved by Meta to write outside the 24 h
window) deliberately carries NO amount: if the number was reassigned, a third
party reads the message and it would reveal a debt. The amount is only shown
after identity verification. In production the customer list would come from an
Athena query over the data lake, not from the demo JSON.

The template text and the suppression `detail` strings stay in Spanish: the first
is what the customer reads, the second is shown to the collections team."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import config
from .store import audit
from .tools import ACTIONS, balance_of, segment_of

PANAMA = ZoneInfo("America/Panama")
CONTACT_HOURS = (8, 19)  # 8:00 to 18:59, Monday to Saturday: allowed contact window
DAYS_BETWEEN_CONTACTS = 3
SYSTEM = "SYSTEM"  # partition key for items that don't belong to a customer (campaigns)
TEMPLATE = {"name": "account_notice_v1", "category": "UTILITY",
            "text": "Hola {{1}}, te escribe el asistente virtual de LLA. Tienes un aviso pendiente sobre "
                    "tu cuenta. Responde a este mensaje para revisarlo. Para no recibir estos avisos, "
                    "responde BAJA."}
# Display names for the operations console (Spanish UI).
SEGMENT_NAMES = {"forgetful": "Olvidadizo", "temporary_hardship": "Dificultad temporal",
                 "repeat_defaulter": "Reincidente", "in_dispute": "En disputa",
                 "high_value_b2b": "Alto valor B2B"}


def panama_now() -> datetime:
    """Business date (AGIL_TODAY, for reproducible demos) with Panama's real time of day."""
    return datetime.combine(config.today(), datetime.now(PANAMA).time(), PANAMA)


def within_hours(now: datetime) -> bool:
    return now.weekday() < 6 and CONTACT_HOURS[0] <= now.hour < CONTACT_HOURS[1]


def last_contact(customer: dict, store) -> str | None:
    """The most recent between the CRM contacts and those recorded by a campaign."""
    dates = [c["date"] for c in customer["history"]["recent_contacts"]]
    # 'date' is the campaign's business date (not the server clock), same as the CRM's.
    dates += [c.get("date", c["created"]) for c in store.find("contacts", customer["customer_id"])]
    return max(dates, default=None)  # ISO: text order is chronological order


def suppression_reason(customer: dict, store, now: datetime, force_hours: bool) -> tuple[str, str] | None:
    """(reason, detail) of the FIRST rule that prevents contact, or None if allowed.
    Order matters: consent and the law first, operations second."""
    cid, h = customer["customer_id"], customer["history"]
    claim = h["open_claims"][0]["ticket"] if h["open_claims"] else None
    cases = store.find("escalations", cid)
    last = last_contact(customer, store) or ""  # "" not None: all details are built at once
    days = (now.date() - date.fromisoformat(last[:10])).days if last else None
    rules = [
        ("no_opt_in", not customer.get("opt_in"), "No dio consentimiento para recibir mensajes por WhatsApp"),
        ("opt_out", customer.get("opt_out"), "Pidió no recibir más avisos (BAJA)"),
        ("outside_hours", not force_hours and not within_hours(now),
         f"Fuera del horario permitido (lun-sáb {CONTACT_HOURS[0]}:00-{CONTACT_HOURS[1]}:00, hora de Panamá)"),
        ("open_claim", claim, f"Reclamo {claim} abierto: no se cobra durante una disputa"),
        ("non_collectable_segment", segment_of(customer) == "high_value_b2b", "Cuenta B2B: la gestiona un ejecutivo"),
        ("open_human_case", cases, f"Caso {cases[-1]['case_id']} en manos de un asesor" if cases else ""),
        ("active_promise", any(p["status"] == "active" for p in store.find("promises", cid)),
         "Tiene una promesa de pago vigente"),
        ("recently_contacted", days is not None and days < DAYS_BETWEEN_CONTACTS,
         f"Contactado el {last[:10]} (mínimo {DAYS_BETWEEN_CONTACTS} días entre contactos)"),
        ("no_balance", balance_of(customer) <= 0, "No tiene saldo vencido"),
    ]
    return next(((reason, detail) for reason, applies, detail in rules if applies), None)


def list_segments(customers: dict) -> list[dict]:
    return [{"id": s, "name": SEGMENT_NAMES[s],
             "estimated_eligible": sum(segment_of(c) == s for c in customers.values())} for s in ACTIONS]


def run_campaign(customers: dict, store, segments: list[str] | None = None,
                 now: datetime | None = None, force_hours: bool = False) -> dict:
    """Evaluates the rules for each customer in the requested segments (all if none
    given) and "sends" the template to the eligible ones: records the contact (for
    the 3-day rule), the message in the visible conversation, and the campaign."""
    now = now or panama_now()
    requested = segments or list(ACTIONS)
    campaign_id = f"CMP-{now:%Y%m%d}-{store.count('campaigns') + 1:04d}"
    results = []
    for cid, c in customers.items():
        if (segment := segment_of(c)) not in requested:
            continue
        row = {"customer_id": cid, "name": c["name"], "segment": segment}
        if suppression := suppression_reason(c, store, now, force_hours):
            results.append({**row, "status": "suppressed", "reason": suppression[0], "detail": suppression[1]})
            continue
        text = TEMPLATE["text"].replace("{{1}}", c["name"].split()[0])
        store.save("contacts", {"customer_id": cid, "campaign_id": campaign_id, "channel": "whatsapp",
                                "date": now.isoformat(timespec="minutes"), "template": TEMPLATE["name"]})
        store.save("conversation", {"customer_id": cid, "role": "campaign", "text": text,
                                    "template": TEMPLATE["name"], "campaign_id": campaign_id})
        audit("template_sent", cid, campaign_id=campaign_id, template=TEMPLATE["name"], text=text)
        results.append({**row, "status": "sent", "text": text})
    by_reason: dict[str, int] = {}
    for r in results:
        if r["status"] == "suppressed":
            by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    summary = {"sent": sum(r["status"] == "sent" for r in results),
               "suppressed": sum(by_reason.values()), "by_reason": by_reason}
    campaign = {"campaign_id": campaign_id, "executed_at": now.replace(tzinfo=None).isoformat(timespec="seconds"),
                "template": TEMPLATE["name"], "segments": requested, "force_hours": force_hours,
                "summary": summary}
    store.save("campaigns", {"customer_id": SYSTEM, **campaign})
    audit("campaign", SYSTEM, **campaign)  # force_hours stays in the audit trail
    return {**campaign, "results": results}
