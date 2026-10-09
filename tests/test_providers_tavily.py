"""Hermetic tests for the Tavily provider adapter.

Network is replaced by monkey-patched `TavilyClient.search` / `extract`
methods on the constructed provider's `_client`. The fake `http_client`
on the lifespan fixture is unused.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from free_web_search_mcp.budget import Budget
from free_web_search_mcp.cache import DiskCache
from free_web_search_mcp.providers import ProviderContext
from free_web_search_mcp.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from free_web_search_mcp.providers.tavily import (
    EMPTY_EXTRACT_NOTE,
    EMPTY_SEARCH_NOTE,
    TavilyAuthError,
    TavilyClient,
    TavilyError,
    TavilyProvider,
    TavilyQuotaError,
    TavilyRateLimitError,
    TavilyUnavailableError,
)
from free_web_search_mcp.rate_limit import MinIntervalLimiter
from free_web_search_mcp.settings import Settings

# --- SPEC §3.2 required keys (pro_search) and §3.5 (extract_urls) ---


SEARCH_REQUIRED_KEYS = {
    "query",
    "provider_used",
    "fallthroughs",
    "answer",
    "results",
    "request_id",
    "cache",
    "note",
    "usage",
}
EXTRACT_REQUIRED_KEYS = {
    "urls",
    "provider_used",
    "fallthroughs",
    "results",
    "failed_results",
    "request_id",
    "cache",
    "note",
    "usage",
}


# --- helpers ---


def _default_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        transport="http",
        host="127.0.0.1",
        port=8000,
        path="/mcp",
        cache_dir=tmp_path,
        http_timeout_s=30.0,
        log_level=40,  # ERROR
        tavily_api_key="tvly-test-key",
        firecrawl_api_key="",
        ddg_enabled=True,
        ddg_cache_ttl_secs=86400,
        ddg_min_interval_ms=0,
        tavily_enabled=True,
        tavily_base_url="https://api.tavily.com",
        tavily_cache_ttl_secs=86400,
        tavily_min_interval_ms=0,
        tavily_max_calls=200,
        tavily_max_raw_chars=8000,
        firecrawl_enabled=False,
        firecrawl_base_url="https://api.firecrawl.dev/v2",
        firecrawl_cache_ttl_secs=86400,
        firecrawl_min_interval_ms=0,
        firecrawl_min_credits=50,
        firecrawl_max_markdown_chars=8000,
    )
    base.update(overrides)
    return Settings(**base)


def _make_ctx(
    tmp_path: Path,
    *,
    tavily_api_key: str = "tvly-test-key",
    tavily_max_calls: int = 200,
    tavily_min_interval_ms: int = 0,
) -> tuple[ProviderContext, Settings]:
    settings = _default_settings(
        tmp_path,
        tavily_api_key=tavily_api_key,
        tavily_max_calls=tavily_max_calls,
        tavily_min_interval_ms=tavily_min_interval_ms,
    )
    http_client = httpx.AsyncClient()
    cache = DiskCache(settings.cache_dir, settings.tavily_cache_ttl_secs)
    rate_limiters = {ProviderName.TAVILY: MinIntervalLimiter(settings.tavily_min_interval_ms)}
    budget = Budget(tavily_configured=bool(settings.tavily_api_key))
    ctx = ProviderContext(
        settings=settings,
        cache=cache,
        rate_limiters=rate_limiters,
        budget=budget,
        http_client=http_client,
    )
    return ctx, settings


def _search_payload() -> dict:
    return {
        "query": "rust programming language",
        "answer": "A multi-paradigm systems language.",
        "results": [
            {
                "title": "Rust",
                "url": "https://www.rust-lang.org",
                "content": "Rust is a systems programming language.",
                "score": 0.97,
                "raw_content": "Welcome to Rust.",
                "published_date": None,
                "favicon": "https://www.rust-lang.org/favicon.ico",
                "id": "result-1",
            }
        ],
        "request_id": "req-search-1",
        "response_time": 0.42,
    }


def _extract_payload() -> dict:
    return {
        "results": [
            {
                "url": "https://example.com/a",
                "raw_content": "Body A",
                "images": ["https://example.com/a.png"],
                "favicon": "https://example.com/a.ico",
            }
        ],
        "failed_results": [
            {"url": "https://example.com/b", "error": "Failed to retrieve content"},
        ],
        "request_id": "req-extract-1",
        "response_time": 0.55,
    }


def _patch_search(provider: TavilyProvider, response: dict | Exception):
    async def fake(*_a, **_kw):
        if isinstance(response, Exception):
            raise response
        return response

    provider._client.search = fake  # type: ignore[assignment]


def _patch_extract(provider: TavilyProvider, response: dict | Exception):
    async def fake(*_a, **_kw):
        if isinstance(response, Exception):
            raise response
        return response

    provider._client.extract = fake  # type: ignore[assignment]


# ============================================================ search shape --


async def test_search_returns_spec_shape(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, _search_payload())

    resp = await p.search(query="Rust", max_results=5, force_refresh=False, depth="basic")
    assert SEARCH_REQUIRED_KEYS.issubset(resp.keys())
    assert resp["provider_used"] == "tavily"
    assert resp["fallthroughs"] == []
    assert resp["cache"] == "miss"
    assert resp["query"] == "Rust"
    assert resp["request_id"] == "req-search-1"
    assert resp["answer"] == "A multi-paradigm systems language."
    assert isinstance(resp["results"], list)
    assert resp["results"][0]["url"] == "https://www.rust-lang.org"
    assert resp["note"] == ""  # non-empty results
    assert resp["usage"]["provider"] == "tavily"
    assert resp["usage"]["calls_this_session"] == 1
    assert resp["usage"]["billing_period_end"] is None


async def test_search_populates_usage_calls_this_session(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, _search_payload())
    ctx.budget.tavily_counter.charge(by=4)

    resp = await p.search(query="Rust", max_results=5, force_refresh=True, depth="advanced")
    # charge(1) added after the call.
    assert resp["usage"]["calls_this_session"] == 5


async def test_search_raises_provider_auth_error_on_401(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, TavilyAuthError("Tavily rejected the API key (401)"))

    with pytest.raises(ProviderAuthError):
        await p.search(query="rust", max_results=5, force_refresh=True)


@pytest.mark.parametrize("status_payload", ["432", "433"])
async def test_search_raises_provider_quota_error_on_432_433(tmp_path: Path, status_payload: str) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, TavilyQuotaError(f"Tavily quota exhausted (HTTP {status_payload})"))

    with pytest.raises(ProviderQuotaError):
        await p.search(query="rust", max_results=5, force_refresh=True)


async def test_search_raises_provider_rate_limit_error_on_429(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, TavilyRateLimitError("rate limited"))

    with pytest.raises(ProviderRateLimitError):
        await p.search(query="rust", max_results=5, force_refresh=True)


async def test_search_raises_provider_unavailable_on_timeout(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, httpx.TimeoutException("connect timeout"))

    with pytest.raises(ProviderUnavailableError):
        await p.search(query="rust", max_results=5, force_refresh=True)


async def test_search_raises_provider_unavailable_on_connect_error(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, httpx.ConnectError("dns failure"))

    with pytest.raises(ProviderUnavailableError):
        await p.search(query="rust", max_results=5, force_refresh=True)


async def test_search_raises_provider_error_on_generic_tavily_error(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, TavilyError("upstream HTTP 500"))

    with pytest.raises(ProviderError):
        await p.search(query="rust", max_results=5, force_refresh=True)


# ============================================================== cache path --


async def test_search_force_refresh_bypasses_cache(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    payload = _search_payload()
    call_count = 0

    async def fake(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        return payload

    p._client.search = fake  # type: ignore[assignment]

    r1 = await p.search(query="rust", max_results=5, force_refresh=False)
    r2 = await p.search(query="rust", max_results=5, force_refresh=False)
    r3 = await p.search(query="rust", max_results=5, force_refresh=True)

    assert call_count == 2  # populated once, force_refresh re-fetched, third hit cache
    assert r1["cache"] == "miss"
    assert r2["cache"] == "hit"
    assert r3["cache"] == "bypassed"


async def test_search_empty_results_returns_note(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    empty = _search_payload()
    empty["results"] = []
    empty["answer"] = ""
    _patch_search(p, empty)

    resp = await p.search(query="nonesuch query", max_results=5, force_refresh=False)
    assert resp["results"] == []
    assert resp["note"] == EMPTY_SEARCH_NOTE


# ============================================================ advisory guard --


async def test_search_advisory_guard_blocks_when_max_calls_reached(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path, tavily_max_calls=3)
    p = TavilyProvider(ctx)
    ctx.budget.tavily_counter.charge(by=3)

    with pytest.raises(ProviderQuotaError, match="FWS_TAVILY_MAX_CALLS"):
        await p.search(query="rust", max_results=5, force_refresh=False)


async def test_search_advisory_guard_disabled_when_max_calls_zero(tmp_path: Path) -> None:
    """FWS_TAVILY_MAX_CALLS=0 disables the advisory guard."""
    ctx, _ = _make_ctx(tmp_path, tavily_max_calls=0)
    p = TavilyProvider(ctx)
    ctx.budget.tavily_counter.charge(by=10_000)
    _patch_search(p, _search_payload())

    # Must not raise; completes the call.
    resp = await p.search(query="rust", max_results=5, force_refresh=True)
    assert resp["provider_used"] == "tavily"


async def test_search_increments_counter_on_success(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_search(p, _search_payload())
    assert ctx.budget.tavily_counter.calls == 0

    await p.search(query="rust", max_results=5, force_refresh=True)
    assert ctx.budget.tavily_counter.calls == 1
    await p.search(query="Rust", max_results=5, force_refresh=True)
    assert ctx.budget.tavily_counter.calls == 2


# ============================================================ supports + cfg --


def test_supports_search_and_fetch_urls_only(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    assert p.supports("search") is True
    assert p.supports("fetch_urls") is True
    assert p.supports("fetch_url") is False
    assert p.supports("chain") is False
    assert p.supports("") is False


def test_configured_false_when_no_api_key(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path, tavily_api_key="")
    p = TavilyProvider(ctx)
    assert p.configured is False


def test_configured_true_when_api_key_present(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path, tavily_api_key="tvly-real-key")
    p = TavilyProvider(ctx)
    assert p.configured is True


# ============================================================ fetch_urls --


async def test_fetch_urls_returns_spec_shape(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_extract(p, _extract_payload())

    resp = await p.fetch_urls(
        urls=["https://example.com/a", "https://example.com/b"], force_refresh=False
    )
    assert EXTRACT_REQUIRED_KEYS.issubset(resp.keys())
    assert resp["provider_used"] == "tavily"
    assert resp["fallthroughs"] == []
    assert resp["cache"] == "miss"
    assert resp["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert resp["request_id"] == "req-extract-1"
    assert resp["results"][0]["url"] == "https://example.com/a"
    assert resp["failed_results"][0]["url"] == "https://example.com/b"
    assert resp["usage"]["provider"] == "tavily"
    assert resp["note"] == ""  # some results, some failed


async def test_fetch_urls_all_failures_returns_extract_note(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    _patch_extract(
        p,
        {
            "results": [],
            "failed_results": [
                {"url": "https://example.com/a", "error": "paywall"},
                {"url": "https://example.com/b", "error": "timeout"},
            ],
            "request_id": "req-extract-fail",
        },
    )

    resp = await p.fetch_urls(urls=["https://example.com/a"], force_refresh=False)
    assert resp["results"] == []
    assert len(resp["failed_results"]) == 2
    assert resp["note"] == ""  # at least one failure entry counts as "not empty"


async def test_fetch_urls_rejects_non_http_scheme(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    with pytest.raises(ProviderError, match="http"):
        await p.fetch_urls(urls=["ftp://example.com/file"], force_refresh=False)


async def test_fetch_urls_rejects_empty_list(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    with pytest.raises(ProviderError):
        await p.fetch_urls(urls=[], force_refresh=False)


async def test_fetch_urls_rejects_more_than_20_urls(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    urls = [f"https://example.com/{i}" for i in range(21)]
    with pytest.raises(ProviderError, match="1-20"):
        await p.fetch_urls(urls=urls, force_refresh=False)


async def test_fetch_urls_force_refresh_bypasses_cache(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    p = TavilyProvider(ctx)
    payload = _extract_payload()
    call_count = 0

    async def fake(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        return payload

    p._client.extract = fake  # type: ignore[assignment]

    r1 = await p.fetch_urls(urls=["https://example.com/a"], force_refresh=False)
    r2 = await p.fetch_urls(urls=["https://example.com/a"], force_refresh=False)
    r3 = await p.fetch_urls(urls=["https://example.com/a"], force_refresh=True)

    assert call_count == 2
    assert r1["cache"] == "miss"
    assert r2["cache"] == "hit"
    assert r3["cache"] == "bypassed"
