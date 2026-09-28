"""Shared fixtures: every test runs against an isolated data dir with no live API keys."""

from __future__ import annotations

import pytest

from app import hindsight, llm
from app.config import settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "hindsight_api_key", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "groq_backoff_base_seconds", 0.0)
    hindsight.set_client(None)
    llm.set_client(None)
    yield
    hindsight.set_client(None)
    llm.set_client(None)
