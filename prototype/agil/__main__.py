"""CLI: `python -m agil demo` (scripted scenarios), `python -m agil chat --customer C001`,
`python -m agil campaign` (proactive contact) and `python -m agil serve` (local web + API,
the same handler that runs on Lambda).
The terminal simulates WhatsApp and shows what is normally invisible: tools,
guardrails and the HANDLED / ESCALATED decision of every turn.
Visible terminal text stays in Spanish (UI for the Panama team)."""
import argparse
import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config
from .agent import Agent
from .llm import create_llm
from .store import Store, load_customers

console = Console()

# Each scenario is a real conversation with the agent, not a recorded answer.
SCENARIOS = [
    ("1. Olvidadizo: pago inmediato con link", "C001",
     ["Hola, soy Ana. Mi cédula termina en 4521, ¿cuánto debo?", "Mándame el link para pagar ahora, por favor"]),
    ("2. Dificultad temporal: promesa rechazada por política y ajustada", "C002",
     ["Buenas, mi cédula termina en 7788. ¿Puedo pagar en 20 días?", "Está bien, pago $50 en 10 días"]),
    ("3. Reincidente: pide 6 cuotas -> rechazo -> insiste -> ESCALA", "C003",
     ["Hola, cédula 1190. Quiero pagar en 6 cuotas", "No, solo puedo en 6 cuotas"]),
    ("4. En disputa -> ESCALA (no se cobra)", "C004",
     ["Me llegó un cobro de roaming que no reconozco, no viajé"]),
    ("5. Vulnerabilidad -> ESCALA con prioridad", "C007",
     ["Mi cédula termina en 5566. Estoy en el hospital y ahora no puedo pagar nada"]),
]
DECISION_LABELS = {"HANDLED": "GESTIONA", "ESCALATED": "ESCALA"}


def show_event(e: dict) -> None:
    if e["type"] == "tool":
        r = e["result"]
        status = "[green]ok[/]" if r["ok"] else f"[red]{r['error']['code']}[/]"
        console.print(f"   [yellow]⚙ {e['name']}[/]({json.dumps(e['input'], ensure_ascii=False)}) → {status}")
        console.print(f"     [dim]{json.dumps(r, ensure_ascii=False)[:220]}[/]")
    elif e["type"] == "guardrail":
        console.print(f"   [magenta]🛡 guardrail: {e['detail']}[/]")
    elif e["type"] == "campaign":
        console.print(f"   [cyan]📣 {e['detail']}[/]")
    elif e["type"] == "verification":
        console.print(f"   [cyan]🔐 {e['detail']}[/]")
    else:
        console.print(f"   [red]⚠ {e['detail']}[/]")


def turn(agent: Agent, customer_id: str, text: str) -> str:
    console.print(Panel(text, title="📱 Cliente", title_align="left", border_style="green", width=90))
    t = agent.respond(customer_id, text)
    color = "red" if t.decision == "ESCALATED" else "blue"
    console.print(Panel(t.reply, title=f"🤖 Asistente LLA · [bold {color}]{DECISION_LABELS[t.decision]}[/]",
                        title_align="left", border_style=color, width=90))
    return t.decision


def new_agent(store: Store, customers: dict) -> Agent:
    return Agent(create_llm(), store, customers, on_event=show_event)


def demo() -> None:
    store, customers = Store(), load_customers()
    store.reset()  # every demo starts clean and is reproducible
    summary = []
    for title, cid, messages in SCENARIOS:
        agent = new_agent(store, customers)
        console.rule(f"[bold]{title}[/] · {customers[cid]['name']} ({cid})")
        decisions = [turn(agent, cid, m) for m in messages]
        summary.append((title, decisions[-1]))
    console.rule("[bold]Resumen")
    for title, decision in summary:
        console.print(f"  {'[red]ESCALA  ' if decision == 'ESCALATED' else '[blue]GESTIONA'}[/]  {title}")
    console.print(f"\n[dim]Store: {store.path} · Auditoría: {config.VAR_DIR / 'audit.jsonl'}[/]")


def chat(customer_id: str) -> None:
    customers = load_customers()
    if customer_id not in customers:
        console.print(f"[red]Cliente desconocido. Opciones: {', '.join(customers)}[/]")
        return
    agent = new_agent(Store(), customers)
    console.print(f"[dim]Conversando como {customers[customer_id]['name']} "
                  f"(pista: cédula {customers[customer_id]['national_id']}). Escribe 'salir' para terminar.[/]")
    while (text := console.input("[green]tú> [/]").strip()).lower() not in ("salir", "exit"):
        if text:
            turn(agent, customer_id, text)


def campaign(segments: list[str] | None, force_hours: bool) -> None:
    from .campaigns import run_campaign
    r = run_campaign(load_customers(), Store(), segments, force_hours=force_hours)
    table = Table(title=f"Campaña {r['campaign_id']} · plantilla {r['template']}"
                        + (" · [yellow]horario forzado (demo)[/]" if force_hours else ""))
    for col in ("Cliente", "Segmento", "Estado", "Motivo / mensaje"):
        table.add_column(col)
    for row in r["results"]:
        sent = row["status"] == "sent"
        table.add_row(f"{row['customer_id']} {row['name']}", row["segment"],
                      "[green]enviado[/]" if sent else "[red]suprimido[/]",
                      row["text"] if sent else f"[bold]{row['reason']}[/]: {row['detail']}")
    console.print(table, f"Resumen: {json.dumps(r['summary'], ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agil", description="Agente de cobranza Ágil (LLA Panamá)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Corre los escenarios guionados")
    p_chat = sub.add_parser("chat", help="Conversación interactiva (simula WhatsApp)")
    p_chat.add_argument("--customer", default="C001")
    p_serve = sub.add_parser("serve", help="Web + API HTTP local (mismo código que la Lambda)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_camp = sub.add_parser("campaign", help="Contacto proactivo: envía la plantilla a los clientes elegibles")
    p_camp.add_argument("--segment", action="append", help="Repetible; por defecto, todos los segmentos")
    p_camp.add_argument("--force-hours", action="store_true", help="Solo demo: ignora la regla de horario")
    args = parser.parse_args()
    if args.command == "campaign":  # rules in code: it doesn't need the brain
        return campaign(args.segment, args.force_hours)
    console.print(Panel(f"Cerebro: [bold]{create_llm().label}[/]", border_style="cyan"))
    if args.command == "serve":
        from .api import serve
        return serve(args.host, args.port)
    demo() if args.command == "demo" else chat(args.customer)


if __name__ == "__main__":
    main()
