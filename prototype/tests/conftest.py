from datetime import date

import pytest

from agil import config
from agil.store import Store, cargar_clientes
from agil.tools import Contexto

HOY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def aislado(tmp_path, monkeypatch):
    """Cada test escribe en un directorio temporal, sin red y con fecha fija."""
    monkeypatch.setattr(config, "DIR_VAR", tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("AGIL_HOY", str(HOY))


@pytest.fixture
def ctx(tmp_path):
    clientes, store = cargar_clientes(), Store(tmp_path / "store.json")
    return lambda cid, verificado=True: Contexto(clientes[cid], store, HOY, verificado)
