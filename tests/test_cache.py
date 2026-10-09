"""Tests for the namespaced `DiskCache`."""
from __future__ import annotations

import time

import pytest

from free_web_search_mcp.cache import DiskCache


def _cache(base, ttl=86400) -> DiskCache:
    return DiskCache(base, ttl)


def test_disabled_when_ttl_zero(tmp_path) -> None:
    cache = _cache(tmp_path, ttl=0)
    assert cache.enabled is False
    assert cache.get("duckduckgo", "x") is None
    cache.set("duckduckgo", "x", "search", {"a": 1})  # no-op
    assert cache.get("duckduckgo", "x") is None


def test_round_trip(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    cached = cache.get("duckduckgo", "rust")
    assert cached is not None
    assert cached.kind == "search"
    assert cached.response == {"results": [{"title": "Rust"}]}


def test_namespace_isolation(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": []})
    cache.set("tavily", "rust", "search", {"results": [{"title": "T"}]})
    d = cache.get("duckduckgo", "rust")
    t = cache.get("tavily", "rust")
    assert d is not None and t is not None
    assert d.response != t.response


def test_stale_entry_falls_through(tmp_path) -> None:
    cache = _cache(tmp_path, ttl=1)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    time.sleep(1.2)
    assert cache.get("duckduckgo", "rust") is None


def test_corrupt_file_does_not_fail(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    p = next((tmp_path / "duckduckgo").glob("*.json"))
    p.write_text("not-json", encoding="utf-8")
    assert cache.get("duckduckgo", "rust") is None  # falls through gracefully


def test_unwritable_dir_does_not_fail(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    # Pre-create the namespace dir and chmod it read-only on POSIX
    import os
    (tmp_path / "duckduckgo").mkdir()
    os.chmod(tmp_path / "duckduckgo", 0o555)
    try:
        cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})  # should not raise
    finally:
        os.chmod(tmp_path / "duckduckgo", 0o755)


def test_atomic_write_through_tmp(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    target = next((tmp_path / "duckduckgo").iterdir())
    # The `.tmp` suffix should be gone after a successful write.
    assert target.suffix == ".json"


def test_dir_size_bytes(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    cache.set("tavily", "rust", "search", {"results": [{"title": "T"}]})
    total = cache.dir_size_bytes()
    assert total > 0
    ddg_only = cache.dir_size_bytes("duckduckgo")
    assert ddg_only > 0
    assert ddg_only < total or total == ddg_only  # either partition or empty Tavily


def test_invalidate_removes_entry(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.set("duckduckgo", "rust", "search", {"results": [{"title": "Rust"}]})
    cache.invalidate("duckduckgo", "rust")
    assert cache.get("duckduckgo", "rust") is None
