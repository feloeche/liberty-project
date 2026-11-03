"""Configuración central. Todo se lee de variables de entorno para que el mismo
código corra en local (offline), contra la Claude API o contra Amazon Bedrock
sin tocar una línea: en producción estas variables vendrían de AppConfig /
Secrets Manager."""
import os
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_CLIENTES = RAIZ / "data" / "clientes.json"
RUTA_PROMPT = RAIZ / "prompts" / "system.md"
DIR_VAR = Path(os.getenv("AGIL_VAR_DIR", RAIZ / "var"))  # store + auditoría locales
DIR_WEB = RAIZ / "web"  # la web estática la sirve el mismo backend (sin CORS)

# "local" = JSON en var/ y sesiones en memoria; "aws" = DynamoDB + CloudWatch Logs.
BACKEND = os.getenv("AGIL_BACKEND", "local").lower()
TABLA_DYNAMO = os.getenv("AGIL_TABLA", "agil-demo")

MODELO = os.getenv("AGIL_MODEL", "claude-sonnet-5-5")
# Inference profile cross-region (us.*): Bedrock enruta entre regiones de EE.UU.
MODELO_BEDROCK = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
REGION_AWS = os.getenv("AWS_REGION", "us-east-2")
# "medium" es un buen punto de partida para conversación con varias tools.
ESFUERZO = os.getenv("AGIL_EFFORT", "medium")
TIMEOUT_LLM_S = float(os.getenv("AGIL_LLM_TIMEOUT", "120"))

# Políticas de negocio (en producción: versionadas en DynamoDB/AppConfig).
# Viven en código y NO en el prompt: el LLM no puede "negociarlas".
POLITICA = {
    "max_dias_promesa": 15,
    "pct_min_parcial": 0.30,
    "cuotas_permitidas": (2, 3),
    "umbral_humano_usd": 500.0,
    "max_pasos_por_turno": 6,  # tope del loop de tools: evita bucles y costos
}


def proveedor() -> str:
    """Elige el cerebro. Sin configuración cae a 'mock' para que el evaluador
    pueda correr el prototipo sin credenciales."""
    elegido = os.getenv("LLM_PROVIDER", "").lower()
    if elegido in ("anthropic", "bedrock", "mock"):
        return elegido
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock"


def hoy() -> date:
    """Fecha 'de negocio'. AGIL_HOY permite fijarla y tener demos reproducibles."""
    valor = os.getenv("AGIL_HOY")
    return date.fromisoformat(valor) if valor else date.today()
