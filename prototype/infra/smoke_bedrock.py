"""Smoke test de Bedrock: UNA llamada mínima con el mismo cliente que usa la Lambda.

Uso (desde prototype/, con el perfil de la demo; no crea recursos, cuesta < USD 0.01):
    AWS_PROFILE=personal-felipe uv run --extra bedrock python infra/smoke_bedrock.py
Verifica tres cosas antes de desplegar: credenciales/permiso bedrock:InvokeModel,
que el inference profile existe en la región, y que `effort` es aceptado."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_PROVIDER", "bedrock")

from agil import config  # noqa: E402
from agil.llm import ClaudeLLM  # noqa: E402
from agil.tools import SCHEMAS  # noqa: E402

llm = ClaudeLLM(bedrock=True)
print(f"→ {llm.etiqueta} · región {config.REGION_AWS} · effort {config.ESFUERZO}")
r = llm.crear("Responde en una sola palabra.", [{"role": "user", "content": "¿Capital de Panamá?"}], SCHEMAS)  # mismas tools que el agente
texto = "".join(b.get("text", "") for b in r.contenido if b["type"] == "text")
print(f"stop_reason={r.stop_reason} · respuesta={texto!r}")
print("effort aceptado" if llm.extra else "effort NO aceptado: se degradó al esfuerzo por defecto")
