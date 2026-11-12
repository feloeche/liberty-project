from datetime import date

import pytest

from agil import config
from agil.store import Store, load_customers
from agil.tools import Context

TODAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test writes to a temporary directory, with no network and a fixed date."""
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGIL_TODAY", str(TODAY))


@pytest.fixture
def ctx(tmp_path):
    customers, store = load_customers(), Store(tmp_path / "store.json")
    return lambda cid, verified=True: Context(customers[cid], store, TODAY, verified)
