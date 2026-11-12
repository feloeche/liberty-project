"""Bedrock smoke test: ONE minimal call with the same client the Lambda uses.

Usage (from prototype/, with the demo profile; creates no resources, costs < USD 0.01):
    AWS_PROFILE=personal-felipe uv run --extra bedrock python infra/smoke_bedrock.py
Checks three things before deploying: credentials/bedrock:InvokeModel permission,
that the inference profile exists in the region, and that `effort` is accepted."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_PROVIDER", "bedrock")

from agil import config  # noqa: E402
from agil.llm import ClaudeLLM  # noqa: E402
from agil.tools import SCHEMAS  # noqa: E402

llm = ClaudeLLM(bedrock=True)
print(f"→ {llm.label} · region {config.AWS_REGION} · effort {config.EFFORT}")
r = llm.create("Answer in a single word.", [{"role": "user", "content": "Capital of Panama?"}], SCHEMAS)  # same tools as the agent
text = "".join(b.get("text", "") for b in r.content if b["type"] == "text")
print(f"stop_reason={r.stop_reason} · reply={text!r}")
print("effort accepted" if llm.extra else "effort NOT accepted: fell back to the default effort")
