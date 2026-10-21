# Modified for Norma.
"""Standalone tests: isolated SDK sessions and mocked HTTP, no backend dependency."""

from collections.abc import Iterator

import pytest

import norma
from norma.settings import configure_settings, get_settings


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NORMA_RUNTIME_URL", "https://runtime.example.test")
    monkeypatch.setenv("NORMA_AGENT_KEY", "test-agent-key")
    original = get_settings().model_dump()
    norma.shutdown()
    yield
    norma.shutdown()
    configure_settings(**original)
