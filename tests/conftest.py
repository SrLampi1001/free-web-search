"""Shared pytest fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache_dir(monkeypatch) -> Path:
    """Per-test cache dir, env-pointed at it, cleaned up after."""
    with tempfile.TemporaryDirectory(prefix="fws-cache-") as d:
        path = Path(d)
        monkeypatch.setenv("FWS_CACHE_DIR", str(path))
        yield path


@pytest.fixture
def isolated_env(monkeypatch):
    """Reset the env to a known clean state for tests that read settings."""
    for key in list(os.environ):
        if key.startswith(("FWS_", "MCP_LOG_LEVEL", "FASTMCP_LOG_LEVEL")):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _per_test_cache(monkeypatch, tmp_path):
    """Every test gets a fresh cache directory; never share with prior runs."""
    monkeypatch.setenv("FWS_CACHE_DIR", str(tmp_path / "fws"))
    monkeypatch.setenv("MCP_LOG_LEVEL", "ERROR")


@pytest.fixture(autouse=True)
def _silence_logs(monkeypatch):
    monkeypatch.setenv("MCP_LOG_LEVEL", "ERROR")
    yield
