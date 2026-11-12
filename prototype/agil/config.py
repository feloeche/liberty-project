"""Central configuration. Everything is read from environment variables so the same
code runs locally (offline), against the Claude API or against Amazon Bedrock
without changing a line: in production these variables would come from AppConfig /
Secrets Manager."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUSTOMERS_PATH = ROOT / "data" / "customers.json"
PROMPT_PATH = ROOT / "prompts" / "system.md"
VAR_DIR = Path(os.getenv("AGIL_VAR_DIR", ROOT / "var"))  # local store + audit
WEB_DIR = ROOT / "web"  # the static web is served by the same backend (no CORS)

# "local" = JSON in var/ and in-memory sessions; "aws" = DynamoDB + CloudWatch Logs.
BACKEND = os.getenv("AGIL_BACKEND", "local").lower()
DYNAMO_TABLE = os.getenv("AGIL_TABLE", "agil-demo")

MODEL = os.getenv("AGIL_MODEL", "claude-sonnet-5-5")
# Cross-region inference profile (us.*): Bedrock routes across US regions.
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
# "medium" is a good starting point for a conversation with several tools.
EFFORT = os.getenv("AGIL_EFFORT", "medium")
LLM_TIMEOUT_S = float(os.getenv("AGIL_LLM_TIMEOUT", "120"))

# Business policies (in production: versioned in DynamoDB/AppConfig).
# They live in code and NOT in the prompt: the LLM cannot "negotiate" them.
POLICY = {
    "max_promise_days": 15,
    "min_partial_pct": 0.30,
    "allowed_installments": (2, 3),
    "human_threshold_usd": 500.0,
    "max_steps_per_turn": 6,  # cap on the tool loop: prevents loops and runaway costs
}


def provider() -> str:
    """Picks the brain. With no configuration it falls back to 'mock' so the
    reviewer can run the prototype without credentials."""
    chosen = os.getenv("LLM_PROVIDER", "").lower()
    if chosen in ("anthropic", "bedrock", "mock"):
        return chosen
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock"


def today() -> date:
    """Business date. AGIL_TODAY pins it for reproducible demos."""
    value = os.getenv("AGIL_TODAY")
    return date.fromisoformat(value) if value else date.today()
