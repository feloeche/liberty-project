"""Persistencia local que simula los servicios de estado de AWS.

- Store  -> DynamoDB (promesas, acuerdos, links, casos escalados).
- audit  -> CloudWatch Logs / transcripciones en S3 (un evento JSON por línea).
Se eligió JSON plano en vez de SQLite porque se puede abrir y leer en la demo.
Con AGIL_BACKEND=aws las mismas interfaces se implementan sobre DynamoDB
(ver dynamo.py); el agente y las tools no notan la diferencia."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config


def ahora() -> str:
    # Microsegundos: el timestamp forma parte de la sort key en DynamoDB y ordena los ítems.
    # UTC con zona explícita (+00:00): sin ella, el navegador interpretaba la hora del
    # servidor (UTC en Lambda) como hora local y la plantilla salía a la 01:01 a. m.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    """Una 'tabla' por entidad, cada ítem lleva cliente_id (la partition key en DynamoDB)."""

    def __init__(self, ruta: Path | None = None):
        self.ruta = ruta or config.DIR_VAR / "store.json"
        self.datos = json.loads(self.ruta.read_text()) if self.ruta.exists() else {}

    def guardar(self, tabla: str, item: dict) -> dict:
        item = {**item, "creado": ahora()}  # el panel de operación ordena por fecha
        self.datos.setdefault(tabla, []).append(item)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps(self.datos, ensure_ascii=False, indent=2))
        return item

    def buscar(self, tabla: str, cliente_id: str) -> list[dict]:
        return [i for i in self.datos.get(tabla, []) if i["cliente_id"] == cliente_id]

    def contar(self, tabla: str) -> int:
        return len(self.datos.get(tabla, []))

    def todos(self, tabla: str) -> list[dict]:
        """Vista de operación (todas las filas de una tabla, de todos los clientes)."""
        return list(self.datos.get(tabla, []))

    def borrar_de(self, tabla: str, cliente_id: str) -> None:
        """'Reiniciar conversación' de un cliente: limpia su chat visible, no sus promesas."""
        self.datos[tabla] = [i for i in self.datos.get(tabla, []) if i["cliente_id"] != cliente_id]
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps(self.datos, ensure_ascii=False, indent=2))

    def reiniciar(self) -> None:
        self.datos = {}
        if self.ruta.exists():
            self.ruta.unlink()


class SesionesLocal:
    """Sesiones en memoria del proceso: suficiente para la CLI y `serve` (un proceso).
    En Lambda no sirve porque cada invocación puede caer en otro contenedor."""

    def __init__(self):
        self.datos: dict[str, dict] = {}

    def cargar(self, cliente_id: str) -> dict | None:
        return self.datos.get(cliente_id)

    def guardar(self, cliente_id: str, sesion: dict) -> None:
        self.datos[cliente_id] = sesion

    def borrar(self, cliente_id: str | None = None) -> None:
        self.datos.pop(cliente_id, None) if cliente_id else self.datos.clear()


def crear_persistencia():
    """(store, sesiones) según AGIL_BACKEND. boto3 solo se importa en modo aws."""
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.DynamoStore(), dynamo.SesionesDynamo()
    return Store(), SesionesLocal()


def auditar(evento: str, cliente_id: str, **datos) -> None:
    """Registro inmutable de TODO lo que entra, sale y cada tool ejecutada.
    Es la base para responder '¿cómo sabes que el agente dijo lo correcto?'."""
    linea = {"ts": ahora(), "evento": evento, "cliente_id": cliente_id, **datos}
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.auditar(linea)
    config.DIR_VAR.mkdir(parents=True, exist_ok=True)
    with open(config.DIR_VAR / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")


def leer_auditoria(cliente_id: str | None, limite: int) -> list[dict]:
    """Eventos más recientes primero (para el panel de traza de la web)."""
    if config.BACKEND == "aws":
        from . import dynamo
        return dynamo.leer_auditoria(cliente_id, limite)
    ruta = config.DIR_VAR / "audit.jsonl"
    lineas = ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []
    eventos = [e for e in map(json.loads, reversed(lineas)) if not cliente_id or e["cliente_id"] == cliente_id]
    return eventos[:limite]


def cargar_clientes() -> dict[str, dict]:
    """Datos mock que simulan la réplica del billing/CRM."""
    datos = json.loads(config.RUTA_CLIENTES.read_text(encoding="utf-8"))
    return {c["cliente_id"]: c for c in datos["clientes"]}
