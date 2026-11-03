"""CLI: `python -m agil demo` (escenarios guionados), `python -m agil chat --cliente C001`,
`python -m agil campana` (contacto proactivo) y `python -m agil serve` (web + API local,
el mismo handler que corre en Lambda).
La terminal simula WhatsApp y muestra lo que normalmente es invisible: tools,
guardrails y la decisión GESTIONA / ESCALA de cada turno."""
import argparse
import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config
from .agent import Agente
from .llm import crear_llm
from .store import Store, cargar_clientes

consola = Console()

# Cada escenario es una conversación real con el agente, no una respuesta grabada.
ESCENARIOS = [
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


def mostrar_evento(e: dict) -> None:
    if e["tipo"] == "tool":
        r = e["resultado"]
        estado = "[green]ok[/]" if r["ok"] else f"[red]{r['error']['codigo']}[/]"
        consola.print(f"   [yellow]⚙ {e['nombre']}[/]({json.dumps(e['entrada'], ensure_ascii=False)}) → {estado}")
        consola.print(f"     [dim]{json.dumps(r, ensure_ascii=False)[:220]}[/]")
    elif e["tipo"] == "guardrail":
        consola.print(f"   [magenta]🛡 guardrail: {e['detalle']}[/]")
    elif e["tipo"] == "campana":
        consola.print(f"   [cyan]📣 {e['detalle']}[/]")
    elif e["tipo"] == "verificacion":
        consola.print(f"   [cyan]🔐 {e['detalle']}[/]")
    else:
        consola.print(f"   [red]⚠ {e['detalle']}[/]")


def turno(agente: Agente, cliente_id: str, texto: str) -> str:
    consola.print(Panel(texto, title="📱 Cliente", title_align="left", border_style="green", width=90))
    t = agente.responder(cliente_id, texto)
    color = "red" if t.decision == "ESCALA" else "blue"
    consola.print(Panel(t.respuesta, title=f"🤖 Asistente LLA · [bold {color}]{t.decision}[/]",
                        title_align="left", border_style=color, width=90))
    return t.decision


def nuevo_agente(store: Store, clientes: dict) -> Agente:
    return Agente(crear_llm(), store, clientes, al_evento=mostrar_evento)


def demo() -> None:
    store, clientes = Store(), cargar_clientes()
    store.reiniciar()  # cada demo parte limpia y es reproducible
    resumen = []
    for titulo, cid, mensajes in ESCENARIOS:
        agente = nuevo_agente(store, clientes)
        consola.rule(f"[bold]{titulo}[/] · {clientes[cid]['nombre']} ({cid})")
        decisiones = [turno(agente, cid, m) for m in mensajes]
        resumen.append((titulo, decisiones[-1]))
    consola.rule("[bold]Resumen")
    for titulo, decision in resumen:
        consola.print(f"  {'[red]ESCALA  ' if decision == 'ESCALA' else '[blue]GESTIONA'}[/]  {titulo}")
    consola.print(f"\n[dim]Store: {store.ruta} · Auditoría: {config.DIR_VAR / 'audit.jsonl'}[/]")


def chat(cliente_id: str) -> None:
    clientes = cargar_clientes()
    if cliente_id not in clientes:
        consola.print(f"[red]Cliente desconocido. Opciones: {', '.join(clientes)}[/]")
        return
    agente = nuevo_agente(Store(), clientes)
    consola.print(f"[dim]Conversando como {clientes[cliente_id]['nombre']} "
                  f"(pista: cédula {clientes[cliente_id]['cedula']}). Escribe 'salir' para terminar.[/]")
    while (texto := consola.input("[green]tú> [/]").strip()).lower() not in ("salir", "exit"):
        if texto:
            turno(agente, cliente_id, texto)


def campana(segmentos: list[str] | None, forzar_horario: bool) -> None:
    from .campanas import ejecutar_campana
    r = ejecutar_campana(cargar_clientes(), Store(), segmentos, forzar_horario=forzar_horario)
    tabla = Table(title=f"Campaña {r['campana_id']} · plantilla {r['plantilla']}"
                        + (" · [yellow]horario forzado (demo)[/]" if forzar_horario else ""))
    for col in ("Cliente", "Segmento", "Estado", "Motivo / mensaje"):
        tabla.add_column(col)
    for f in r["resultados"]:
        enviado = f["estado"] == "enviado"
        tabla.add_row(f"{f['cliente_id']} {f['nombre']}", f["segmento"],
                      "[green]enviado[/]" if enviado else "[red]suprimido[/]",
                      f["texto"] if enviado else f"[bold]{f['motivo']}[/]: {f['detalle']}")
    consola.print(tabla, f"Resumen: {json.dumps(r['resumen'], ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agil", description="Agente de cobranza Ágil (LLA Panamá)")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("demo", help="Corre los escenarios guionados")
    p_chat = sub.add_parser("chat", help="Conversación interactiva (simula WhatsApp)")
    p_chat.add_argument("--cliente", default="C001")
    p_serve = sub.add_parser("serve", help="Web + API HTTP local (mismo código que la Lambda)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_camp = sub.add_parser("campana", help="Contacto proactivo: envía la plantilla a los clientes elegibles")
    p_camp.add_argument("--segmento", action="append", help="Repetible; por defecto, todos los segmentos")
    p_camp.add_argument("--forzar-horario", action="store_true", help="Solo demo: ignora la regla de horario")
    args = parser.parse_args()
    if args.comando == "campana":  # reglas en código: no necesita el cerebro
        return campana(args.segmento, args.forzar_horario)
    consola.print(Panel(f"Cerebro: [bold]{crear_llm().etiqueta}[/]", border_style="cyan"))
    if args.comando == "serve":
        from .api import servir
        return servir(args.host, args.port)
    demo() if args.comando == "demo" else chat(args.cliente)


if __name__ == "__main__":
    main()
