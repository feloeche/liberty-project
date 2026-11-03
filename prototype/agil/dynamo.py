"""Backend AWS (AGIL_BACKEND=aws): UNA tabla DynamoDB para todo el estado.

Diseño single-table (pk = cliente_id), porque todos los accesos del agente son
"lo de este cliente" y así cada turno se resuelve con Query por partición:
  sk = "<tabla>#<ts>#<id>"  -> promesas, acuerdos, links, escalamientos (Store)
  sk = "SESION"             -> memoria de la conversación, con TTL de 24 h
                               (la ventana de servicio de WhatsApp)
  sk = "AUDIT#<ts>#<id>"    -> auditoría que lee /api/auditoria
El contenido va como JSON en el atributo `datos`: DynamoDB exige Decimal en vez
de float, y serializar evita convertir montos en cada lectura y escritura.
Simplificaciones de demo: las vistas globales (operación, contar casos) usan un
Scan acotado; en producción serían un GSI y un contador atómico, y la sesión
llevaría bloqueo optimista (versión) contra turnos concurrentes."""
import json
import time
import uuid
from functools import cache

from . import config
from .store import ahora

TTL_SESION_S = 24 * 3600
MAX_PAGINAS_SCAN = 5  # tope de costo: la tabla de la demo es pequeña


@cache
def tabla():
    import boto3  # viene incluido en el runtime de Lambda
    return boto3.resource("dynamodb").Table(config.TABLA_DYNAMO)


def _sk(prefijo: str) -> str:
    return f"{prefijo}#{ahora()}#{uuid.uuid4().hex[:8]}"


def _query(cliente_id: str, prefijo: str, **kw) -> list[dict]:
    from boto3.dynamodb.conditions import Key
    r = tabla().query(KeyConditionExpression=Key("pk").eq(cliente_id) & Key("sk").begins_with(prefijo), **kw)
    return [json.loads(i["datos"]) for i in r["Items"]]


def _scan(**kw):
    """Scan paginado y acotado (generador de ítems crudos)."""
    for _ in range(MAX_PAGINAS_SCAN):
        r = tabla().scan(**kw)
        yield from r["Items"]
        if "LastEvaluatedKey" not in r:
            return
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


class DynamoStore:
    """Misma interfaz que store.Store, sobre DynamoDB."""

    def guardar(self, t: str, item: dict) -> dict:
        item = {**item, "creado": ahora()}
        tabla().put_item(Item={"pk": item["cliente_id"], "sk": _sk(t), "tabla": t,
                               "datos": json.dumps(item, ensure_ascii=False)})
        return item

    def buscar(self, t: str, cliente_id: str) -> list[dict]:
        return _query(cliente_id, f"{t}#")

    def contar(self, t: str) -> int:
        from boto3.dynamodb.conditions import Attr
        return sum(1 for _ in _scan(FilterExpression=Attr("tabla").eq(t), ProjectionExpression="pk"))

    def todos(self, t: str) -> list[dict]:
        from boto3.dynamodb.conditions import Attr
        return [json.loads(i["datos"]) for i in _scan(FilterExpression=Attr("tabla").eq(t))]

    def borrar_de(self, t: str, cliente_id: str) -> None:
        from boto3.dynamodb.conditions import Key
        r = tabla().query(KeyConditionExpression=Key("pk").eq(cliente_id) & Key("sk").begins_with(f"{t}#"),
                          ProjectionExpression="pk, sk")
        with tabla().batch_writer() as lote:
            for i in r["Items"]:
                lote.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})

    def reiniciar(self) -> None:
        """Borra el estado de negocio y las sesiones; la auditoría se conserva."""
        from boto3.dynamodb.conditions import Attr
        with tabla().batch_writer() as lote:
            for i in _scan(FilterExpression=~Attr("sk").begins_with("AUDIT#"), ProjectionExpression="pk, sk"):
                lote.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})


class SesionesDynamo:
    """La Lambda es stateless: la sesión se carga al inicio del turno y se guarda al final."""

    def cargar(self, cliente_id: str) -> dict | None:
        item = tabla().get_item(Key={"pk": cliente_id, "sk": "SESION"}).get("Item")
        # El TTL de DynamoDB borra con retraso (hasta horas): se valida también al leer.
        return json.loads(item["datos"]) if item and item["ttl"] > time.time() else None

    def guardar(self, cliente_id: str, sesion: dict) -> None:
        tabla().put_item(Item={"pk": cliente_id, "sk": "SESION", "ttl": int(time.time()) + TTL_SESION_S,
                               "datos": json.dumps(sesion, ensure_ascii=False, default=str)})

    def borrar(self, cliente_id: str | None = None) -> None:
        if cliente_id:
            tabla().delete_item(Key={"pk": cliente_id, "sk": "SESION"})
        else:
            DynamoStore().reiniciar()  # incluye las sesiones de todos


def auditar(linea: dict) -> None:
    """Doble destino: stdout de Lambda = CloudWatch Logs (un JSON por línea,
    consultable con Logs Insights) y la tabla, para que la web muestre la traza."""
    texto = json.dumps(linea, ensure_ascii=False, default=str)
    print(texto, flush=True)
    tabla().put_item(Item={"pk": linea["cliente_id"], "sk": _sk("AUDIT"), "datos": texto})


def leer_auditoria(cliente_id: str | None, limite: int) -> list[dict]:
    if cliente_id:
        return _query(cliente_id, "AUDIT#", ScanIndexForward=False, Limit=limite)
    from boto3.dynamodb.conditions import Attr
    eventos = [json.loads(i["datos"]) for i in _scan(FilterExpression=Attr("sk").begins_with("AUDIT#"))]
    return sorted(eventos, key=lambda e: e["ts"], reverse=True)[:limite]
