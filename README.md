# Proyecto Ágil — Agentes de IA para la gestión de clientes por WhatsApp

**AI Engineer · Technical Assessment · Liberty Latin America (Panamá)**
Autor: Felipe Echeverri — noviembre 2026

Este repositorio contiene mi [propuesta de arquitectura](docs/Propuesta_Arquitectura_Agil.pdf) y un prototipo funcional de un agente de IA que conversa con clientes que tienen facturas vencidas. El agente consulta su saldo (datos ficticios), decide si **gestiona** el caso (link de pago, promesa o acuerdo) o lo **escala** a una persona, y deja todo registrado.

**Principio de diseño:** *el LLM conversa, el código decide.* Montos, plazos, cuotas, a quién contactar y cuándo escalar viven en código determinístico y testeado. El modelo redacta y elige herramientas, pero no puede saltarse una regla del negocio.

---

## Correr el prototipo (≈ 2 minutos, sin credenciales)

**Requisitos:** Python 3.11 o superior y [uv](https://docs.astral.sh/uv/getting-started/installation/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). No necesitas cuentas, API keys ni AWS.

```bash
cd prototype
uv run python -m agil demo      # 5 conversaciones de punta a punta
uv run pytest                   # 56 tests
```

El primer comando instala las dependencias en un entorno aislado y corre cinco escenarios:

| # | Cliente (ficticio) | Situación | Decisión esperada |
|---|---|---|---|
| 1 | C001 · Ana | Olvidó pagar | **GESTIONA**: genera un link de pago |
| 2 | C002 · Carlos | Pide 20 días; la política permite 15 | **GESTIONA**: rechazo por política y promesa ajustada |
| 3 | C003 · Luis | Reincidente, pide 6 cuotas e insiste | **ESCALA** a cobranza humana |
| 4 | C004 · María | Disputa un cobro de roaming | **ESCALA** a reclamos, sin cobrar |
| 5 | C007 · Jorge | Está en el hospital | **ESCALA** con prioridad (SLA 2 h) |

En la salida verás cada mensaje, cada herramienta que el agente invoca (⚙), los filtros de seguridad (🛡), la verificación de identidad (🔐) y la decisión final.

### Más formas de probarlo

```bash
cd prototype
uv run python -m agil chat --customer C002         # conversación libre en la terminal
uv run python -m agil campaign --force-hours       # contacto proactivo: quién recibe aviso y quién no
uv run python -m agil serve --port 8000            # web en http://127.0.0.1:8000
```

La **web local** tiene tres partes:
- un chat tipo WhatsApp;
- el panel **"Cómo decidió el agente"**, con cada herramienta, filtro y decisión;
- la **consola de operación** (`/operations`): casos escalados con cola y SLA, promesas, links, auditoría y el botón **Lanzar campaña**.

Cada cliente muestra la pista de su cédula, para que puedas verificarte (los datos son ficticios).

**Sin `uv`:** `cd prototype && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, y luego los mismos comandos sin `uv run`.

### Modo offline vs. Claude real

Sin credenciales, el agente corre en **modo offline**: un motor de reglas determinístico que imita las decisiones del modelo y emite llamadas a herramientas reales por el mismo camino. **No es un LLM.** Existe para que el prototipo corra en cualquier máquina y para que los tests sean reproducibles. Lo indica la pantalla ("OFFLINE" / "MOCK offline").

Para usar Claude de verdad:

| Opción | Cómo |
|---|---|
| Claude API | `export ANTHROPIC_API_KEY=...` y los mismos comandos |
| Amazon Bedrock | `uv sync --extra bedrock`, luego `LLM_PROVIDER=bedrock AWS_REGION=us-east-2 AWS_PROFILE=<perfil>` |

---

## Qué hay en el repositorio

```
.
├── README.md                          ← este archivo
├── docs/
│   └── Propuesta_Arquitectura_Agil.pdf ← propuesta de arquitectura y estrategia
└── prototype/
    ├── agil/                          ← código del agente (Python)
    │   ├── agent.py                   ← orquestador de un turno (verificar → filtros → ciclo de tools → escalar)
    │   ├── tools.py                   ← las 6 herramientas y sus políticas de negocio
    │   ├── guardrails.py              ← filtros antes y después del modelo
    │   ├── campaigns.py               ← contacto proactivo: elegibilidad y plantilla
    │   ├── llm.py                     ← Claude API, Bedrock o modo offline (misma interfaz)
    │   ├── api.py                     ← API HTTP: la misma función en local y en Lambda
    │   └── store.py, dynamo.py        ← estado local (JSON) o en DynamoDB
    ├── prompts/system.md              ← instrucciones del agente
    ├── data/customers.json            ← 7 clientes ficticios (uno por segmento)
    ├── web/                           ← chat, panel de decisiones y consola de operación
    ├── infra/                         ← AWS SAM: Lambda, HTTP API, DynamoDB, Scheduler, IAM
    └── tests/                         ← políticas, filtros, campañas, API, DynamoDB y demo e2e
```

## Cómo responde este entregable a lo que pide el caso

| El caso pide | Dónde está |
|---|---|
| ¿Cómo sabe el agente con quién hablar y qué decirle? | Propuesta §4 · `prototype/agil/campaigns.py` (elegibilidad y plantilla) |
| ¿Cómo funciona por dentro: razona, qué puede y qué no, memoria? | Propuesta §5 · `agent.py`, `tools.py`, `prompts/system.md` |
| ¿Cómo se integra con los sistemas existentes? | Propuesta §6 · `tools.py` (lectura/escritura), `dynamo.py` |
| ¿Cómo se gestiona y controla: campañas, reglas? | Propuesta §7 · consola de operación y `config.POLICY` |
| ¿Qué puede salir mal y cómo se mitiga? | Propuesta §9 (14 riesgos) · `guardrails.py` y los tests |
| ¿Cómo sabes si funciona: métricas? | Propuesta §10 y §11 (grupo de control, evals) |
| 100 % AWS, con justificación de cada servicio | Propuesta §3 y §12 · `prototype/infra/template.yaml` |
| Agentes de IA, no un árbol de decisiones | Ciclo de tool use con Claude (`agent.py`, `llm.py`) |
| Flujo mínimo: mensaje simulado → saldo → gestionar o escalar | `uv run python -m agil demo` |
| Al menos una herramienta (tool use) | 6 herramientas: `get_balance`, `get_history`, `create_payment_link`, `register_payment_promise`, `offer_payment_plan`, `escalate_to_human` |
| README ejecutable | Este archivo: tres comandos y ninguna credencial |

## Supuestos y límites del prototipo

- **Ficticio:** los datos de clientes, montos y links de pago son inventados, y todo número de negocio de la propuesta es un supuesto declarado.
- **Canal:** el WhatsApp es simulado (web y terminal). La integración real (AWS End User Messaging Social y plantillas aprobadas por Meta) está descrita en la propuesta §3–§4.
- **Identidad:** la verificación con los últimos 4 dígitos de la cédula es proporcional para una demo. En producción la reemplazaría un link autenticado (propuesta §17).
- **Filtros:** los de seguridad son reglas legibles, la primera de tres capas. En producción se suman Bedrock Guardrails y un clasificador (propuesta §5.2).
- **Qué está simplificado:** la relación entre el prototipo y la arquitectura objetivo está en la propuesta §16, y lo que haría distinto en producción, en §17.

## Si algo no funciona

- **`uv: command not found`:** instala uv con el comando de arriba y abre una terminal nueva, o usa la ruta con `pip`.
- **La demo usa la API real sin querer:** si tienes `ANTHROPIC_API_KEY` en tu entorno, el agente usa Claude. Para forzar el modo offline: `LLM_PROVIDER=mock uv run python -m agil demo`.
- **Volver a empezar:** `rm -rf prototype/var`. Ahí se guarda el estado local de las corridas.
