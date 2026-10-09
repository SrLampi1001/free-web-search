"""Hermetic tests for the routing Strategy (SPEC §6 decision matrix)."""
from __future__ import annotations

import pytest

from free_web_search_mcp.providers.base import ProviderName
from free_web_search_mcp.providers.router import NoProviderError, pick


# --- FakeProvider (inline; no production import) ----------------------------


class FakeProvider:
    """Minimal stand-in for a configured ProviderABC; never called here."""

    def __init__(self, name: ProviderName) -> None:
        self.name = name
        self.configured = True

    def supports(self, operation: str) -> bool:  # pragma: no cover - unused
        return True

    async def search(self, **_kwargs):  # pragma: no cover - unused
        return {}

    async def fetch_url(self, **_kwargs):  # pragma: no cover - unused
        return {}

    async def fetch_urls(self, **_kwargs):  # pragma: no cover - unused
        return {}


def _providers(*names: ProviderName) -> dict[ProviderName, FakeProvider]:
    return {n: FakeProvider(n) for n in names}


def test_pick_entity_returns_duckduckgo_when_configured() -> None:
    p = _providers(ProviderName.DUCKDUCKGO, ProviderName.TAVILY)
    assert pick("entity", p) == [ProviderName.DUCKDUCKGO]


def test_pick_general_returns_tavily_then_firecrawl_when_both_configured() -> None:
    p = _providers(ProviderName.TAVILY, ProviderName.FIRECRAWL, ProviderName.DUCKDUCKGO)
    assert pick("general", p) == [ProviderName.TAVILY, ProviderName.FIRECRAWL]


def test_pick_general_with_only_tavily_returns_tavily() -> None:
    p = _providers(ProviderName.TAVILY)
    assert pick("general", p) == [ProviderName.TAVILY]


def test_pick_general_with_only_firecrawl_returns_firecrawl() -> None:
    p = _providers(ProviderName.FIRECRAWL)
    assert pick("general", p) == [ProviderName.FIRECRAWL]


def test_pick_general_with_no_providers_returns_empty() -> None:
    assert pick("general", {}) == []


def test_pick_deep_returns_firecrawl_then_tavily() -> None:
    p = _providers(ProviderName.FIRECRAWL, ProviderName.TAVILY)
    assert pick("deep", p) == [ProviderName.FIRECRAWL, ProviderName.TAVILY]


def test_pick_scrape_returns_firecrawl_when_configured_else_empty() -> None:
    assert pick("scrape", _providers(ProviderName.FIRECRAWL)) == [ProviderName.FIRECRAWL]
    assert pick("scrape", _providers(ProviderName.TAVILY)) == []


def test_pick_extract_returns_tavily_when_configured_else_empty() -> None:
    assert pick("extract", _providers(ProviderName.TAVILY)) == [ProviderName.TAVILY]
    assert pick("extract", _providers(ProviderName.FIRECRAWL)) == []


def test_pick_status_returns_empty_regardless_of_providers() -> None:
    assert pick("status", {}) == []
    assert pick("status", _providers(ProviderName.DUCKDUCKGO)) == []
    assert pick("status", _providers(ProviderName.TAVILY, ProviderName.FIRECRAWL)) == []


def test_no_provider_error_is_a_provider_error() -> None:
    """Defined here so callers can distinguish routing failure from adapter failure."""
    from free_web_search_mcp.providers.base import ProviderError

    assert issubclass(NoProviderError, ProviderError)


def test_pick_unknown_intent_returns_empty() -> None:
    assert pick("nonsense", _providers(ProviderName.TAVILY)) == []
