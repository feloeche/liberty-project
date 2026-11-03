# Prototipo Ágil — detalle técnico

> **¿Solo quieres correrlo?** Las instrucciones están en el [README principal](../README.md#correr-el-prototipo--2-minutos-sin-credenciales): son dos comandos y no requieren credenciales. Este documento explica **cómo está construido y por qué**.

El prototipo es la parte ejecutable de la [propuesta de arquitectura](../docs/PROPUESTA_ARQUITECTURA.md). Usa las mismas herramientas, segmentos y reglas de escalamiento (§1.2, §5.3 y §5.6 de la propuesta).

---

## 1. Cómo procesa un mensaje

```
mensaje entrante
   │
   ▼
agent.py ── 1. verificación de identidad (código) ──────────────┐
   │        2. filtro de entrada (guardrails.py)                 │
   │        3. ciclo de tool use ⇄ llm.py (Claude | Bedrock | offline)
   │                 │                                           │
   │                 ▼                                           │
   │             tools.py (políticas determinísticas) ─▶ store   │
   │        4. escalamiento garantizado si una regla lo exige    │
   │        5. filtro de salida → mensaje seguro si falla        │
   ▼                                                             ▼
respuesta + decisión GESTIONA / ESCALA                 auditoría (todo queda registrado)
```

| Módulo | Responsabilidad | En producción (AWS) |
|---|---|---|
| `agent.py` | Orquesta el turno, la memoria de sesión y la decisión | Bedrock AgentCore Runtime (o Lambda); sesión en DynamoDB con TTL |
| `tools.py` | Las 6 herramientas y sus políticas | Lambdas detrás de AgentCore Gateway → billing/CRM por API interna (PrivateLink); reglas en AppConfig |
| `guardrails.py` | Filtros de entrada y salida | Esta capa determinística **más** Bedrock Guardrails y un clasificador |
| `campanas.py` | Contacto proactivo: elegibilidad y plantilla | EventBridge Scheduler → lista desde Athena → AWS End User Messaging Social |
| `llm.py` | Interfaz única hacia el modelo | Amazon Bedrock (Claude) |
| `api.py` | API HTTP: una función pura `handle()` con dos adaptadores (Lambda y `http.server`) | API Gateway + Lambda |
| `store.py` / `dynamo.py` | Estado: promesas, acuerdos, links, casos, sesiones, conversación y auditoría | DynamoDB + CloudWatch Logs (+ S3 con KMS) |
| `prompts/system.md` | Tono, privacidad, continuidad y reglas de escalamiento | Prompt versionado y aprobado (§7.3 de la propuesta) |

`AGIL_BACKEND=local` (por defecto) guarda todo en `var/` (JSON). `AGIL_BACKEND=aws` usa DynamoDB. El código del agente es el mismo en los dos casos.

### Los tres "cerebros"

| Modo | Cómo se activa | Qué es |
|---|---|---|
| **Offline** | Por defecto si no hay `ANTHROPIC_API_KEY`, o con `LLM_PROVIDER=mock` | Un motor de reglas que **imita** las decisiones del modelo y emite `tool_use` reales por el mismo camino. **No es un LLM:** sirve para correr sin credenciales y para tests reproducibles |
| **Claude API** | `ANTHROPIC_API_KEY=...` · modelo en `AGIL_MODEL` (por defecto `claude-sonnet-5-5`) | Claude decide qué herramienta usar y qué responder |
| **Amazon Bedrock** | `uv sync --extra bedrock` y `LLM_PROVIDER=bedrock AWS_REGION=us-east-2` · modelo en `BEDROCK_MODEL_ID` (por defecto `us.anthropic.claude-sonnet-4-6`) | La ruta de producción: el mismo código, autorizado con IAM y sin API keys |

El modelo es una variable de entorno, no está acoplado al diseño. La demo desplegada usa Sonnet 4.6 porque es el que está habilitado en la cuenta; cambiarlo es cambiar un parámetro y volver a correr los tests y la demo.

---

## 2. Decisiones de diseño (y por qué)

1. **Las políticas viven en las herramientas, no en el prompt.**
   - **Las reglas:** promesa a máximo 15 días, abono parcial de al menos 30 %, una sola promesa activa, cuotas de 2 o 3 solo si la campaña y el segmento lo permiten, y sin cobro en disputa ni en B2B.
   - **Si el modelo pide algo fuera de política:** la herramienta devuelve un error estructurado (`codigo`, `mensaje`, `alternativas`) y el modelo tiene que reconducir. Una prompt injection no puede regalar 6 cuotas.
2. **El código entrega los números de la política ya calculados.** `consultar_saldo` devuelve `limites_politica` (mínimo parcial, fecha máxima, valor de cada cuota), solo con las acciones que ese segmento puede usar. Así el modelo no hace aritmética y todo monto que menciona viene de una herramienta.
3. **El escalamiento obligatorio lo garantiza el código.** El filtro de entrada detecta disputa, vulnerabilidad, pedido de una persona, amenaza legal o ACODECO, e insultos persistentes. Si el modelo no escala, el orquestador escala igual. Lo mismo ante una falla de la API, un `refusal` o los pasos agotados: el cliente nunca queda sin respuesta.
4. **El filtro de salida ataca la peor alucinación en cobranza: el monto inventado.**
   - **Montos:** todo monto que el agente escribe tiene que haber salido de una herramienta en esa sesión.
   - **Otros bloqueos:** amenazas, y ofertas fuera de política; la regla distingue una negativa ("las 6 cuotas no están disponibles") de una oferta.
   - **Si un mensaje falla:** no se envía. Sale un mensaje seguro y se escala.
5. **Privacidad (Ley 81 de 2019).**
   - **Sin verificar no hay saldo:** se piden los últimos 4 dígitos de la cédula. La verificación la hace el código; el modelo no puede "decidir" que alguien está verificado.
   - **Un cliente por sesión:** el `cliente_id` sale de la sesión, no de lo que mande el modelo, así que no puede consultar a otro cliente.
   - **Minimización:** las herramientas no devuelven cédula ni teléfono, y el resumen para el asesor enmascara los dígitos.
6. **Contacto proactivo con reglas en código y plantilla sin monto.**
   - **Reglas, en orden** (gana la primera que aplica): sin opt-in, opt-out, fuera de horario (lunes a sábado, 8:00–19:00 Panamá), reclamo abierto, B2B, caso humano abierto, promesa vigente, contacto en los últimos 3 días, sin saldo.
   - **La plantilla no lleva monto:** si el número fue reasignado, revelaría una deuda ajena. El detalle aparece solo después de verificar identidad.
   - **Continuidad:** la plantilla entra a la conversación del modelo como **su propio mensaje anterior**, así el agente continúa el hilo en lugar de volver a presentarse.
7. **Ciclo de tool use propio, no el tool runner de la SDK.** El modo offline tiene que recorrer exactamente el mismo camino que Claude, y cada llamada se intercepta para auditar, actualizar la lista blanca de montos y forzar escalamientos. El ciclo tiene un tope de 6 pasos por turno.
8. **Sin frameworks web.** `api.py` usa solo la librería estándar: la Lambda pesa menos, arranca más rápido y el mismo `handle()` corre en local y en AWS.

---

## 3. Qué simplifiqué y qué haría en producción

| En el prototipo | En producción |
|---|---|
| Filtros con palabras clave y regex | Tres capas: estas reglas + Bedrock Guardrails + clasificador de intención y riesgo; revisión humana muestral |
| Verificación en un paso (últimos 4 dígitos) | Link autenticado al portal o la app (u OTP) según el riesgo; límite de intentos |
| Sesión en DynamoDB con TTL, sin bloqueo | Bloqueo optimista por versión y SQS FIFO por conversación |
| Vistas de operación con Scan acotado | GSI y contadores atómicos |
| Link de pago ficticio (`pagos.lla.example`) | Pasarela real con webhook que cierra la promesa |
| Segmentación con umbrales fijos | Modelo de propensión (SageMaker, fase 3) con las reglas como límite duro |
| Políticas como constantes en `config.py` | Reglas versionadas en AppConfig, con aprobación desde la consola |
| Campaña lanzada a mano sobre datos ficticios | Scheduler diario, lista desde Athena, revalidación contra billing, envío real con plantillas aprobadas por Meta y BAJA como opt-out automático |
| 5 escenarios guionados | Golden set de 200–300 casos, LLM-as-judge calibrado y red teaming en cada cambio de prompt o modelo |
| Reintentos con backoff (3) y escalamiento si el modelo falla | Además: modelo de respaldo, alarmas en CloudWatch y X-Ray |
| Web pública protegida con un token | Webhook firmado por Meta; consola con Cognito y SSO |

---

## 4. Despliegue en AWS

Una Lambda sirve la web y la API detrás de un HTTP API. El estado vive en una tabla DynamoDB y el modelo corre en Bedrock. La infraestructura está en `infra/template.yaml` (AWS SAM) y SAM se usa con `uvx`, sin instalarlo.

```bash
cd prototype/infra
python3 -c "import secrets; print(secrets.token_urlsafe(18))" > .demo-token   # token de acceso (ignorado por git)
make build                                                                     # código + web + dependencias Linux ARM64
uvx --from aws-sam-cli sam deploy --template-file template.yaml --guided \
    --parameter-overrides "DemoToken=$(cat .demo-token) LlmProvider=bedrock"
uvx --from aws-sam-cli sam list stack-outputs --stack-name agil-demo         # URL → abrir con ?t=<token>
uvx --from aws-sam-cli sam delete --stack-name agil-demo                     # borra todo al terminar
```

- **Antes del primer despliegue:** `AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-2 uv run --extra bedrock python infra/smoke_bedrock.py` hace una llamada mínima al modelo. El primer uso de un modelo de Anthropic en una cuenta exige enviar una vez el formulario de caso de uso. Se hace desde la consola de Bedrock, abriendo el modelo en el *playground*.
- **Sin costo de modelo:** `LlmProvider=mock`.
- **Región:** `us-east-2`, la que permite la política de la cuenta de demo. Bedrock se invoca con el inference profile `us.*`, que enruta dentro de EE. UU.
- **Por qué `make build` y no `sam build`:** la copia de `sam build` omitía el binario de `pydantic-core` y la Lambda fallaba al importar. Se verificó con la imagen oficial `public.ecr.aws/lambda/python:3.12-arm64`. `make build` descarga con `uv` los wheels Linux ARM64 desde macOS, sin Docker. `boto3` no se empaqueta porque ya viene en el runtime.
- **Tope de 30 s del HTTP API:** por eso en AWS el esfuerzo del modelo es `low` y cada llamada tiene un timeout de 25 s. Si el modelo no responde, el agente escala.
- **Costo:** en reposo, prácticamente USD 0, porque nada tiene costo fijo. Por turno, unos USD 0,02–0,06 en Bedrock; una sesión de 100 turnos ronda los USD 5. El límite de 5 peticiones por segundo y el token acotan el gasto.

| Recurso SAM | Componente de la propuesta | Nota |
|---|---|---|
| `AgilApi` (HTTP API) | API Gateway del webhook | En producción: End User Messaging Social → SQS FIFO |
| `AgilFunction` (Lambda Python 3.12, arm64) | Orquestador y herramientas | También sirve la web estática (demo sin CloudFront) |
| `AgilTabla` (DynamoDB, `pk = cliente_id`) | Estado del agente | Una sola tabla: `sk` = `<tabla>#ts#id`, `SESION` (TTL 24 h), `AUDIT#ts#id` |
| `CampanaDiaria` (EventBridge Scheduler, `DISABLED`) | Disparador de campañas (§4.2) | Lunes a sábado 9:00 Panamá; solo puede invocar esta Lambda |
| `AgilLogs` (CloudWatch Logs, 14 días) | Auditoría | Un JSON por línea, consultable con Logs Insights |
| Política IAM de Bedrock | Amazon Bedrock (Claude) | Limitada a modelos `anthropic.*` y al inference profile `us.anthropic.*` |

---

## 5. Estructura

```
prototype/
├── agil/
│   ├── __main__.py   # CLI: demo, chat, campana y serve
│   ├── agent.py      # orquestador del turno
│   ├── tools.py      # 6 herramientas + políticas
│   ├── guardrails.py # filtros de entrada y salida
│   ├── campanas.py   # contacto proactivo
│   ├── llm.py        # Claude API | Bedrock | offline
│   ├── api.py        # HTTP: handle() + adaptadores Lambda y http.server
│   ├── store.py      # estado local (JSON) y auditoría
│   ├── dynamo.py     # estado en DynamoDB
│   └── config.py     # políticas y variables de entorno
├── prompts/system.md
├── data/clientes.json   # 7 clientes ficticios
├── web/                 # chat, panel de decisiones y consola de operación
├── infra/               # template.yaml, Makefile, samconfig.toml, smoke_bedrock.py
└── tests/               # 55 tests, sin red (DynamoDB simulado con moto)
```
