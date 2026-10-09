"""Tests for the orchestrator server: tool list shape, tool call flows.

Phase A shipped the FastMCP skeleton and asserted only on `list_tools()` /
`list_resources()` shape. Phase D adds per-tool flow tests that drive the
real implementations against a hermetic FakeProvider set and a fake Budget.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from free_web_search_mcp.budget import Budget
from free_web_search_mcp.providers.base import (
    ProviderABC,
    ProviderError,
    ProviderName,
    ProviderUnavailableError,
)
from free_web_search_mcp.server import (
    INSTRUCTIONS,
    credit_status,
    deep_search,
    entity_search,
    extract_urls,
    mcp,
    pro_search,
    scrape_url,
)


# --- list-shape smoke tests (Phase A preserved) ---------------------------


@pytest.mark.asyncio
async def test_lists_six_tools() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(
        ["entity_search", "pro_search", "deep_search",
         "scrape_url", "extract_urls", "credit_status"]
    )


@pytest.mark.asyncio
async def test_lists_three_prompts_until_phase_e() -> None:
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
    names = sorted(p.name for p in prompts)
    assert names == sorted(["route_query", "research_brief", "audit_budget"])


@pytest.mark.asyncio
async def test_lists_four_resources_until_phase_e() -> None:
    async with Client(mcp) as client:
        resources = await client.list_resources()
    uris = sorted(str(r.uri) for r in resources)
    assert uris == sorted([
        "providers://status",
        "providers://budget",
        "providers://schema",
        "cache://stats",
    ])


def test_instructions_present() -> None:
    assert "Six tools" in INSTRUCTIONS
    assert "Three prompts" in INSTRUCTIONS
    assert "Four resources" in INSTRUCTIONS


# --- FakeProvider ---------------------------------------------------------


class FakeProvider:
    """Adapter stub mirroring test_chain.py::FakeProvider but with finer
    per-method return / raise configuration so the facade's response shape
    is realistic enough to exercise the wrapper's normalisation logic."""

    def __init__(
        self,
        name: ProviderName,
        *,
        search: dict | BaseException | None = None,
        fetch_url: dict | BaseException | None = None,
        fetch_urls: dict | BaseException | None = None,
        supports_map: dict[str, bool] | None = None,
    ) -> None:
        self.name = name
        self.configured = True
        self._search = search if search is not None else {"results": [], "note": ""}
        self._fetch_url = fetch_url if fetch_url is not None else {"markdown": ""}
        self._fetch_urls = fetch_urls if fetch_urls is not None else {"results": [], "failed_results": []}
        self._supports_map = supports_map or {"search": True, "fetch_url": True, "fetch_urls": True}

    def supports(self, operation: str) -> bool:
        return self._supports_map.get(operation, False)

    async def search(self, **_kwargs) -> dict:
        return self._invoke(self._search, "search")

    async def fetch_url(self, **_kwargs) -> dict:
        return self._invoke(self._fetch_url, "fetch_url")

    async def fetch_urls(self, **_kwargs) -> dict:
        return self._invoke(self._fetch_urls, "fetch_urls")

    @staticmethod
    def _invoke(value: dict | BaseException, op: str) -> dict:
        if isinstance(value, BaseException):
            raise value
        return value


def _state_with(providers: dict[ProviderName, ProviderABC], budget: Budget | None = None) -> SimpleNamespace:
    """Stub Context whose `request_context.lifespan_context` matches the
    real lifespan shape: keys the tool bodies read."""
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={
                "providers": providers,
                "budget": budget or Budget(),
                "settings": None,
                "cache": None,
                "rate_limiters": {},
                "http_client": None,
            }
        )
    )


def _stub_ddg(returns: dict | BaseException | None = None) -> FakeProvider:
    return FakeProvider(
        ProviderName.DUCKDUCKGO,
        search=returns if returns is not None else {
            "query": "rust",
            "results": [{"title": "Rust", "url": "https://rust-lang.org", "snippet": "language"}],
            "note": "",
        },
        supports_map={"search": True, "fetch_url": False, "fetch_urls": False},
    )


# --- entity_search --------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_search_happy_path() -> None:
    ddg = _stub_ddg(
        returns={
            "query": "rust",
            "results": [{"title": "Rust", "url": "https://rust-lang.org", "snippet": "language"}],
            "note": "",
        }
    )
    ctx = _state_with({ProviderName.DUCKDUCKGO: ddg})
    result = await entity_search(query="rust", ctx=ctx)
    assert result["query"] == "rust"
    assert result["provider_used"] == "duckduckgo"
    assert result["results"][0]["title"] == "Rust"
    assert result["fallthroughs"] == []


@pytest.mark.asyncio
async def test_entity_search_empty_query_returns_degenerate_shape() -> None:
    # No providers configured: empty-query branch must NOT call the facade.
    ctx = _state_with({})
    result = await entity_search(query="", ctx=ctx)
    assert result["results"] == []
    assert result["note"] == "no query, nothing was searched"


@pytest.mark.asyncio
async def test_entity_search_empty_results_get_standard_note() -> None:
    ddg = _stub_ddg(returns={"query": "zzz", "results": [], "note": ""})
    ctx = _state_with({ProviderName.DUCKDUCKGO: ddg})
    result = await entity_search(query="zzz", ctx=ctx)
    assert result["results"] == []
    assert result["note"].startswith("<empty:")


@pytest.mark.asyncio
async def test_entity_search_clamps_count_to_10() -> None:
    captured: dict[str, Any] = {}

    class Capture(FakeProvider):
        async def search(self, *, query, max_results, force_refresh, **kw):  # type: ignore[override]
            captured["max_results"] = max_results
            return {"query": query, "results": [], "note": ""}

    ctx = _state_with({ProviderName.DUCKDUCKGO: Capture(ProviderName.DUCKDUCKGO)})
    await entity_search(query="x", count=999, ctx=ctx)
    assert captured["max_results"] == 10

    await entity_search(query="x", count=0, ctx=ctx)
    assert captured["max_results"] == 1


@pytest.mark.asyncio
async def test_entity_search_provider_error_becomes_tool_error() -> None:
    ddg = _stub_ddg(returns=ProviderError("upstream borked"))
    ctx = _state_with({ProviderName.DUCKDUCKGO: ddg})
    with pytest.raises(ToolError) as ei:
        await entity_search(query="x", ctx=ctx)
    assert "Provider error" in str(ei.value)
    assert "upstream borked" in str(ei.value)


@pytest.mark.asyncio
async def test_entity_search_unavailable_becomes_tool_error() -> None:
    ddg = _stub_ddg(returns=ProviderUnavailableError("timeout"))
    ctx = _state_with({ProviderName.DUCKDUCKGO: ddg})
    with pytest.raises(ToolError) as ei:
        await entity_search(query="x", ctx=ctx)
    assert "transport error" in str(ei.value).lower()


# --- pro_search -----------------------------------------------------------


@pytest.mark.asyncio
async def test_pro_search_happy_path() -> None:
    tavily = FakeProvider(
        ProviderName.TAVILY,
        search={
            "results": [{"title": "A", "url": "https://a", "content": "c"}],
            "answer": "ok",
            "note": "",
        },
    )
    ctx = _state_with({ProviderName.TAVILY: tavily})
    result = await pro_search(query="what is x", ctx=ctx)
    assert result["provider_used"] == "tavily"
    assert result["answer"] == "ok"
    assert result["results"][0]["title"] == "A"


@pytest.mark.asyncio
async def test_pro_search_empty_query_returns_degenerate_shape() -> None:
    # No providers configured: empty-query branch must NOT call the facade.
    ctx = _state_with({})
    result = await pro_search(query="", ctx=ctx)
    assert result["results"] == []
    assert result["note"] == "no query, nothing was searched"


@pytest.mark.asyncio
async def test_pro_search_empty_results_get_standard_note() -> None:
    tavily = FakeProvider(
        ProviderName.TAVILY,
        search={"results": [], "note": ""},
    )
    ctx = _state_with({ProviderName.TAVILY: tavily})
    result = await pro_search(query="nobody knows", ctx=ctx)
    assert result["results"] == []
    assert result["note"].startswith("<empty:")


@pytest.mark.asyncio
async def test_pro_search_provider_error_becomes_tool_error() -> None:
    tavily = FakeProvider(ProviderName.TAVILY, search=ProviderError("bad request"))
    ctx = _state_with({ProviderName.TAVILY: tavily})
    with pytest.raises(ToolError) as ei:
        await pro_search(query="q", ctx=ctx)
    assert "Provider error" in str(ei.value)


@pytest.mark.asyncio
async def test_pro_search_chain_exhausted_becomes_tool_error() -> None:
    # No providers at all; router returns [], chain raises ChainExhaustedError.
    ctx = _state_with({})
    with pytest.raises(ToolError) as ei:
        await pro_search(query="q", ctx=ctx)
    assert "refused" in str(ei.value).lower() or "no provider" in str(ei.value).lower()


# --- deep_search ----------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_search_happy_path() -> None:
    firecrawl = FakeProvider(
        ProviderName.FIRECRAWL,
        search={
            "results": [{"title": "D", "url": "https://d", "markdown": "body"}],
            "credits_used_this_call": 2,
            "note": "",
        },
    )
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    result = await deep_search(query="how to configure x", ctx=ctx)
    assert result["provider_used"] == "firecrawl"
    assert result["results"][0]["title"] == "D"
    assert result["credits_used_this_call"] == 2


@pytest.mark.asyncio
async def test_deep_search_empty_query_returns_degenerate_shape() -> None:
    # No providers configured: empty-query branch must NOT call the facade.
    ctx = _state_with({})
    result = await deep_search(query="", ctx=ctx)
    assert result["results"] == []
    assert result["note"] == "no query, nothing was searched"


@pytest.mark.asyncio
async def test_deep_search_provider_error_becomes_tool_error() -> None:
    firecrawl = FakeProvider(ProviderName.FIRECRAWL, search=ProviderError("markdown parser fail"))
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    with pytest.raises(ToolError) as ei:
        await deep_search(query="q", ctx=ctx)
    assert "Provider error" in str(ei.value)


@pytest.mark.asyncio
async def test_deep_search_chain_exhausted_becomes_tool_error() -> None:
    ctx = _state_with({})
    with pytest.raises(ToolError) as ei:
        await deep_search(query="q", ctx=ctx)
    msg = str(ei.value).lower()
    assert "refused" in msg or "no provider" in msg


# --- scrape_url -----------------------------------------------------------


@pytest.mark.asyncio
async def test_scrape_url_happy_path() -> None:
    firecrawl = FakeProvider(
        ProviderName.FIRECRAWL,
        fetch_url={
            "url": "https://example.com",
            "final_url": "https://example.com",
            "markdown": "body",
            "title": "Example",
            "metadata": {"title": "Example"},
            "credits_used_this_call": 1,
        },
    )
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    result = await scrape_url(url="https://example.com", ctx=ctx)
    assert result["markdown"] == "body"
    assert result["title"] == "Example"


@pytest.mark.asyncio
async def test_scrape_url_rejects_non_http_url() -> None:
    firecrawl = FakeProvider(ProviderName.FIRECRAWL)
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    with pytest.raises(ToolError) as ei:
        await scrape_url(url="javascript:alert(1)", ctx=ctx)
    assert "http(s)" in str(ei.value)


@pytest.mark.asyncio
async def test_scrape_url_rejects_empty_url() -> None:
    firecrawl = FakeProvider(ProviderName.FIRECRAWL)
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    with pytest.raises(ToolError):
        await scrape_url(url="", ctx=ctx)


@pytest.mark.asyncio
async def test_scrape_url_provider_error_becomes_tool_error() -> None:
    firecrawl = FakeProvider(ProviderName.FIRECRAWL, fetch_url=ProviderError("blocked"))
    ctx = _state_with({ProviderName.FIRECRAWL: firecrawl})
    with pytest.raises(ToolError) as ei:
        await scrape_url(url="https://example.com", ctx=ctx)
    assert "Provider error" in str(ei.value)


# --- extract_urls ---------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_urls_happy_path() -> None:
    tavily = FakeProvider(
        ProviderName.TAVILY,
        fetch_urls={
            "results": [{"url": "https://a", "raw_content": "x"}],
            "failed_results": [],
        },
    )
    ctx = _state_with({ProviderName.TAVILY: tavily})
    result = await extract_urls(urls=["https://a"], ctx=ctx)
    assert result["results"][0]["url"] == "https://a"
    assert result["failed_results"] == []


@pytest.mark.asyncio
async def test_extract_urls_rejects_non_http() -> None:
    tavily = FakeProvider(ProviderName.TAVILY)
    ctx = _state_with({ProviderName.TAVILY: tavily})
    with pytest.raises(ToolError) as ei:
        await extract_urls(urls=["file:///etc/passwd"], ctx=ctx)
    assert "http(s)" in str(ei.value)


@pytest.mark.asyncio
async def test_extract_urls_rejects_empty_list() -> None:
    tavily = FakeProvider(ProviderName.TAVILY)
    ctx = _state_with({ProviderName.TAVILY: tavily})
    with pytest.raises(ToolError) as ei:
        await extract_urls(urls=[], ctx=ctx)
    assert "non-empty" in str(ei.value)


@pytest.mark.asyncio
async def test_extract_urls_rejects_more_than_20() -> None:
    tavily = FakeProvider(ProviderName.TAVILY)
    ctx = _state_with({ProviderName.TAVILY: tavily})
    too_many = [f"https://example.com/{i}" for i in range(21)]
    with pytest.raises(ToolError) as ei:
        await extract_urls(urls=too_many, ctx=ctx)
    assert "1–20" in str(ei.value) or "1-20" in str(ei.value)


@pytest.mark.asyncio
async def test_extract_urls_provider_error_becomes_tool_error() -> None:
    tavily = FakeProvider(ProviderName.TAVILY, fetch_urls=ProviderError("bad batch"))
    ctx = _state_with({ProviderName.TAVILY: tavily})
    with pytest.raises(ToolError) as ei:
        await extract_urls(urls=["https://a"], ctx=ctx)
    assert "Provider error" in str(ei.value)


# --- credit_status --------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_status_returns_snapshot_and_called_at_unix() -> None:
    budget = Budget(tavily_configured=True, firecrawl_configured=True, ddg_configured=True)
    budget.tavily_counter.charge(by=3)
    budget.firecrawl_tracker.hydrate({"remaining_credits": 500, "plan_credits": 1000})
    ctx = _state_with({}, budget=budget)
    result = await credit_status(ctx=ctx)
    assert "providers" in result
    assert result["providers"]["tavily"]["configured"] is True
    assert result["providers"]["firecrawl"]["remaining_credits"] == 500
    assert result["providers"]["duckduckgo"]["configured"] is True
    assert "recommendation" in result
    assert "called_at_unix" in result
    assert isinstance(result["called_at_unix"], float)


@pytest.mark.asyncio
async def test_credit_status_with_no_keys() -> None:
    ctx = _state_with({}, budget=Budget())
    result = await credit_status(ctx=ctx)
    assert result["providers"]["tavily"]["configured"] is False
    assert result["providers"]["firecrawl"]["configured"] is False
    assert result["providers"]["duckduckgo"]["configured"] is True