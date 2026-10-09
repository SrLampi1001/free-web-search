"""Hermetic tests for the Firecrawl provider adapter.

Pattern: instantiate `FirecrawlProvider(ctx)` against a fake `ProviderContext`,
then monkeypatch the upstream `FirecrawlClient.search` / `FirecrawlClient.scrape`
class methods to a controllable `FakeFirecrawl`. Mirrors the sibling's
`tests/test_server.py` mock-at-the-boundary pattern; no network is exercised.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import free_web_search_mcp.providers.firecrawl as fc_module
from free_web_search_mcp.budget import Budget
from free_web_search_mcp.cache import DiskCache
from free_web_search_mcp.providers import ProviderContext, ProviderName
from free_web_search_mcp.providers.firecrawl import (
    EMPTY_SCRAPE_NOTE,
    EMPTY_SEARCH_NOTE,
    FirecrawlAuthError,
    FirecrawlCreditsError,
    FirecrawlError,
    FirecrawlProvider,
    FirecrawlRateLimitError,
)
from free_web_search_mcp.rate_limit import MinIntervalLimiter
from free_web_search_mcp.settings import Settings


# -- Fake upstream ------------------------------------------------------------


class FakeFirecrawl:
    """Pluggable canned responses keyed by query/url. Patches the
    `FirecrawlClient` class methods so the lifespan's client routes every
    call here. Mirrors the sibling's conftest pattern.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.exceptions: dict[tuple[str, str], BaseException] = {}
        self.search_calls: list[tuple[str, int, bool]] = []
        self.scrape_calls: list[str] = []

    def set_search(self, query: str, payload: dict[str, Any]) -> None:
        self.responses[("search", query)] = payload

    def set_scrape(self, url: str, payload: dict[str, Any]) -> None:
        self.responses[("scrape", url)] = payload

    def set_search_error(self, query: str, exc: BaseException) -> None:
        self.exceptions[("search", query)] = exc

    def set_scrape_error(self, url: str, exc: BaseException) -> None:
        self.exceptions[("scrape", url)] = exc

    async def search(self, query: str, limit: int = 5, *, include_markdown: bool = True) -> dict[str, Any]:
        self.search_calls.append((query, limit, include_markdown))
        if ("search", query) in self.exceptions:
            raise self.exceptions[("search", query)]
        return self.responses.get(("search", query), {
            "results": [], "credits_used_this_call": 0, "web_raw_count": 0,
        })

    async def scrape(self, url: str) -> dict[str, Any]:
        self.scrape_calls.append(url)
        if ("scrape", url) in self.exceptions:
            raise self.exceptions[("scrape", url)]
        return self.responses.get(("scrape", url), {
            "markdown": "", "markdown_chars": 0, "markdown_truncated_chars": 0,
            "truncated": False, "title": "", "description": "", "language": "",
            "source_url": url, "final_url": url, "status_code": 200,
            "metadata": {}, "credits_used_this_call": 0,
        })


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def base_settings() -> Settings:
    """Minimal settings with Firecrawl credentials and a tiny cap for testing."""
    return Settings(
        transport="http",
        host="127.0.0.1",
        port=8000,
        path="/mcp",
        cache_dir=Path("/tmp/fws-test-cache"),
        http_timeout_s=10.0,
        log_level=40,  # ERROR
        tavily_api_key="",
        firecrawl_api_key="fc-test-key",
        ddg_enabled=True,
        ddg_cache_ttl_secs=86400,
        ddg_min_interval_ms=0,
        tavily_enabled=False,
        tavily_base_url="https://api.tavily.com",
        tavily_cache_ttl_secs=86400,
        tavily_min_interval_ms=0,
        tavily_max_calls=200,
        tavily_max_raw_chars=8000,
        firecrawl_enabled=True,
        firecrawl_base_url="https://api.firecrawl.dev/v2",
        firecrawl_cache_ttl_secs=86400,
        firecrawl_min_interval_ms=0,
        firecrawl_min_credits=50,
        firecrawl_max_markdown_chars=8000,
    )


@pytest.fixture
def cache(tmp_path: Path) -> DiskCache:
    return DiskCache(tmp_path / "fws", ttl_seconds=86400)


@pytest.fixture
def budget() -> Budget:
    b = Budget(firecrawl_configured=True)
    b.firecrawl_tracker.hydrate({
        "remaining_credits": 500,
        "plan_credits": 1000,
        "billing_period_end": "2026-11-01T00:00:00+00:00",
    })
    return b


@pytest.fixture
def rate_limiters() -> dict[ProviderName, MinIntervalLimiter]:
    return {ProviderName.FIRECRAWL: MinIntervalLimiter(0)}


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


@pytest.fixture
def ctx(
    base_settings: Settings,
    cache: DiskCache,
    budget: Budget,
    rate_limiters: dict[ProviderName, MinIntervalLimiter],
    http_client: httpx.AsyncClient,
) -> ProviderContext:
    return ProviderContext(
        settings=base_settings,
        cache=cache,
        rate_limiters=rate_limiters,
        budget=budget,
        http_client=http_client,
    )


@pytest.fixture
def fake_firecrawl(monkeypatch):
    """Patch the FirecrawlClient class methods onto a controllable fake."""
    fake = FakeFirecrawl()

    async def _search(self, query: str, limit: int = 5, *, include_markdown: bool = True) -> dict[str, Any]:
        return await fake.search(query, limit, include_markdown=include_markdown)

    async def _scrape(self, url: str) -> dict[str, Any]:
        return await fake.scrape(url)

    monkeypatch.setattr(fc_module.FirecrawlClient, "search", _search)
    monkeypatch.setattr(fc_module.FirecrawlClient, "scrape", _scrape)
    return fake


@pytest.fixture
def provider(ctx: ProviderContext, fake_firecrawl: FakeFirecrawl) -> FirecrawlProvider:
    return FirecrawlProvider(ctx)


# -- Test helpers ------------------------------------------------------------


def _seed_search_payload(query: str, *, credits: int = 5, n: int = 1) -> dict[str, Any]:
    """Build an upstream-shaped /v2/search response carrying FULL untruncated markdown."""
    return {
        "results": [
            {
                "title": f"Result {i} for {query}",
                "url": f"https://example.com/{query}/{i}",
                "description": f"desc {i}",
                "markdown": "x" * 12_000,  # large, would normally trigger truncation
                "markdown_chars": 12_000,
                "markdown_truncated_chars": 12_000,  # mapper's view with huge cap
                "truncated": False,
            }
            for i in range(n)
        ],
        "credits_used_this_call": credits,
        "web_raw_count": n,
    }


def _seed_scrape_payload(url: str, *, credits: int = 1) -> dict[str, Any]:
    """Build an upstream-shaped /v2/scrape response with FULL untruncated markdown.

    The metadata uses snake_case to match the SPEC §3.4 projection; the
    adapter passes the mapped `metadata` dict through verbatim.
    """
    return {
        "markdown": "y" * 12_000,  # large body, mapper will not truncate at PRESERVE_CHARS
        "markdown_chars": 12_000,
        "markdown_truncated_chars": 12_000,
        "truncated": False,
        "title": "Example",
        "description": "An example page",
        "language": "en",
        "source_url": url,
        "final_url": url,
        "status_code": 200,
        "metadata": {
            "title": "Example",
            "description": "An example page",
            "language": "en",
            "source_url": url,
            "url": url,
            "status_code": 200,
        },
        "credits_used_this_call": credits,
    }


# -- Tests: configured flag and supports() ----------------------------------


def test_configured_true_when_api_key_set(provider: FirecrawlProvider) -> None:
    assert provider.configured is True
    assert provider.name == ProviderName.FIRECRAWL


def test_supports_contract(provider: FirecrawlProvider) -> None:
    assert provider.supports("search") is True
    assert provider.supports("fetch_url") is True
    assert provider.supports("fetch_urls") is False
    assert provider.supports("other") is False


# -- Tests: search() shape and propagation -----------------------------------


@pytest.mark.asyncio
async def test_search_returns_spec_shape(provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl) -> None:
    fake_firecrawl.set_search("rust", _seed_search_payload("rust", credits=7))
    resp = await provider.search(query="rust", max_results=5, force_refresh=False)

    # SPEC §3.3 required keys
    for key in ("query", "provider_used", "fallthroughs", "results",
                "credits_used_this_call", "usage", "cache", "note"):
        assert key in resp, f"missing key {key!r}"
    assert resp["provider_used"] == "firecrawl"
    assert resp["fallthroughs"] == []
    assert resp["query"] == "rust"
    assert resp["cache"] == "miss"
    assert resp["note"] == ""
    assert resp["credits_used_this_call"] == 7
    # per-result shape
    r0 = resp["results"][0]
    for k in ("title", "url", "description", "markdown", "markdown_chars",
              "markdown_truncated_chars", "truncated"):
        assert k in r0
    # usage shape
    u = resp["usage"]
    assert u["provider"] == "firecrawl"
    assert u["source"] == "live"
    # credit propagation is verified in test_search_propagates_credits_to_budget
    assert u["remaining_credits"] == 500 - 7


@pytest.mark.asyncio
async def test_search_propagates_credits_to_budget(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, budget: Budget,
) -> None:
    fake_firecrawl.set_search("tokio", _seed_search_payload("tokio", credits=7))
    before = budget.firecrawl_tracker.remaining_credits
    await provider.search(query="tokio", max_results=5, force_refresh=False)
    after = budget.firecrawl_tracker.remaining_credits
    assert after == before - 7


@pytest.mark.asyncio
async def test_search_raises_provider_auth_error_on_401(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderAuthError
    fake_firecrawl.set_search_error("rust", FirecrawlAuthError("401 Unauthorized"))
    with pytest.raises(ProviderAuthError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_raises_provider_quota_error_on_402(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderQuotaError
    fake_firecrawl.set_search_error("rust", FirecrawlCreditsError("out of credits"))
    with pytest.raises(ProviderQuotaError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_raises_provider_quota_error_on_insufficient_credits(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    """The body-level `insufficient_credits` code path also maps to Quota."""
    from free_web_search_mcp.providers.base import ProviderQuotaError
    fake_firecrawl.set_search_error("rust", FirecrawlCreditsError("insufficient_credits"))
    with pytest.raises(ProviderQuotaError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_raises_provider_rate_limit_error_on_429(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderRateLimitError
    fake_firecrawl.set_search_error("rust", FirecrawlRateLimitError("429"))
    with pytest.raises(ProviderRateLimitError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_raises_provider_unavailable_on_timeout(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderUnavailableError
    fake_firecrawl.set_search_error("rust", httpx.TimeoutException("read timed out"))
    with pytest.raises(ProviderUnavailableError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_raises_provider_error_on_other_firecrawl_error(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderError
    fake_firecrawl.set_search_error("rust", FirecrawlError("upstream 500"))
    with pytest.raises(ProviderError):
        await provider.search(query="rust", max_results=5, force_refresh=False)


@pytest.mark.asyncio
async def test_search_honors_force_refresh(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, ctx: ProviderContext,
) -> None:
    fake_firecrawl.set_search("rust", _seed_search_payload("rust", credits=3))
    # First call populates the cache.
    r1 = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert r1["cache"] == "miss"
    # Second call with no force_refresh hits the cache; no upstream call added.
    r2 = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert r2["cache"] == "hit"
    assert len(fake_firecrawl.search_calls) == 1
    # Third call with force_refresh=True bypasses cache; cache flag is "bypassed".
    r3 = await provider.search(query="rust", max_results=5, force_refresh=True)
    assert r3["cache"] == "bypassed"
    assert len(fake_firecrawl.search_calls) == 2


@pytest.mark.asyncio
async def test_search_honors_min_credits_guard(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, budget: Budget, ctx: ProviderContext,
) -> None:
    """When the local credit counter is below the configured minimum, the
    adapter converts the resulting `ToolBudgetError` into `ProviderQuotaError`.
    """
    from free_web_search_mcp.providers.base import ProviderQuotaError
    budget.firecrawl_tracker.hydrate({"remaining_credits": 10, "plan_credits": 1000})  # < 50
    fake_firecrawl.set_search("rust", _seed_search_payload("rust"))
    with pytest.raises(ProviderQuotaError):
        await provider.search(query="rust", max_results=5, force_refresh=False)
    # The upstream was never reached; no network call was made.
    assert fake_firecrawl.search_calls == []


@pytest.mark.asyncio
async def test_search_guard_noop_when_probe_never_succeeded(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, budget: Budget,
) -> None:
    """When the tracker is in 'credits unknown' state (probe never succeeded),
    `guard()` is a no-op and the real upstream response surfaces normally.
    """
    budget.firecrawl_tracker.mark_probe_failed("network error")
    fake_firecrawl.set_search("rust", _seed_search_payload("rust", credits=2))
    resp = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert resp["cache"] == "miss"
    assert resp["credits_used_this_call"] == 2


@pytest.mark.asyncio
async def test_search_caches_full_untruncated_markdown(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, ctx: ProviderContext, base_settings: Settings,
) -> None:
    """The cache stores the FULL markdown; re-querying with a different
    `firecrawl_max_markdown_chars` reuses the cached body and re-truncates
    at presentation time (SPEC §7).
    """
    fake_firecrawl.set_search("rust", _seed_search_payload("rust", credits=4))
    # First call with the default cap (8000) — body is truncated to 8000.
    r1 = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert r1["cache"] == "miss"
    assert r1["results"][0]["markdown_chars"] == 12_000  # full body preserved
    assert r1["results"][0]["truncated"] is True
    assert len(r1["results"][0]["markdown"]) <= 8000

    # Switch the cap to 20_000 and re-query from cache (no force_refresh).
    ctx.settings = replace(ctx.settings, firecrawl_max_markdown_chars=20_000)
    r2 = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert r2["cache"] == "hit"
    assert r2["results"][0]["markdown_chars"] == 12_000
    # The full 12_000-char body now fits under 20_000, so it's no longer truncated.
    assert r2["results"][0]["truncated"] is False
    assert r2["results"][0]["markdown_truncated_chars"] == 12_000
    # The body is the FULL 12_000 characters (the truncation marker is gone).
    assert "x" * 12_000 in r2["results"][0]["markdown"]
    # And no upstream call was made.
    assert len(fake_firecrawl.search_calls) == 1


@pytest.mark.asyncio
async def test_search_empty_results_sets_guidance_note(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    fake_firecrawl.set_search("oblivion", {
        "results": [], "credits_used_this_call": 0, "web_raw_count": 0,
    })
    resp = await provider.search(query="oblivion", max_results=5, force_refresh=False)
    assert resp["results"] == []
    assert resp["note"] == EMPTY_SEARCH_NOTE


@pytest.mark.asyncio
async def test_search_unconfigured_provider_raises(
    base_settings: Settings, cache: DiskCache, rate_limiters: dict[ProviderName, MinIntervalLimiter],
    http_client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Without FIRECRAWL_API_KEY the provider refuses with a clear ProviderError."""
    from free_web_search_mcp.providers.base import ProviderError
    unconfigured_settings = replace(base_settings, firecrawl_api_key="")
    unconfigured_budget = Budget(firecrawl_configured=False)
    ctx = ProviderContext(
        settings=unconfigured_settings,
        cache=cache,
        rate_limiters=rate_limiters,
        budget=unconfigured_budget,
        http_client=http_client,
    )
    # Patch the upstream client methods so a misconfiguration doesn't reach the network.
    async def _noop_search(self, query, limit=5, *, include_markdown=True):
        return {"results": [], "credits_used_this_call": 0, "web_raw_count": 0}
    async def _noop_scrape(self, url):
        return {}
    monkeypatch.setattr(fc_module.FirecrawlClient, "search", _noop_search)
    monkeypatch.setattr(fc_module.FirecrawlClient, "scrape", _noop_scrape)
    provider = FirecrawlProvider(ctx)
    assert provider.configured is False
    with pytest.raises(ProviderError) as exc:
        await provider.search(query="rust", max_results=5, force_refresh=False)
    assert "FIRECRAWL_API_KEY" in str(exc.value)


# -- Tests: fetch_url() ------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_url_returns_spec_shape(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    url = "https://example.com/docs"
    fake_firecrawl.set_scrape(url, _seed_scrape_payload(url, credits=2))
    resp = await provider.fetch_url(url=url, force_refresh=False)

    # SPEC §3.4 required keys
    for key in ("url", "final_url", "title", "markdown", "markdown_chars",
                "markdown_truncated_chars", "truncated", "metadata",
                "credits_used_this_call", "usage", "cache", "note"):
        assert key in resp, f"missing key {key!r}"
    assert resp["url"] == url
    assert resp["cache"] == "miss"
    assert resp["credits_used_this_call"] == 2
    # full body preserved, truncated at presentation time
    assert resp["markdown_chars"] == 12_000
    assert resp["truncated"] is True
    assert len(resp["markdown"]) <= 8000
    # metadata projected
    md = resp["metadata"]
    for k in ("title", "description", "language", "source_url", "url", "status_code"):
        assert k in md
    # usage shape
    assert resp["usage"]["provider"] == "firecrawl"
    assert resp["usage"]["source"] == "live"


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_url(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderError
    for bad in ("ftp://example.com/file", "javascript:alert(1)", "file:///etc/passwd", "not a url"):
        with pytest.raises(ProviderError) as exc:
            await provider.fetch_url(url=bad, force_refresh=False)
        assert "http(s)" in str(exc.value).lower() or "http" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_fetch_url_charges_budget(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl, budget: Budget,
) -> None:
    url = "https://example.com/docs"
    fake_firecrawl.set_scrape(url, _seed_scrape_payload(url, credits=2))
    before = budget.firecrawl_tracker.remaining_credits
    await provider.fetch_url(url=url, force_refresh=False)
    after = budget.firecrawl_tracker.remaining_credits
    assert after == before - 2


@pytest.mark.asyncio
async def test_fetch_url_honors_force_refresh(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    url = "https://example.com/docs"
    fake_firecrawl.set_scrape(url, _seed_scrape_payload(url))
    r1 = await provider.fetch_url(url=url, force_refresh=False)
    assert r1["cache"] == "miss"
    r2 = await provider.fetch_url(url=url, force_refresh=False)
    assert r2["cache"] == "hit"
    assert len(fake_firecrawl.scrape_calls) == 1
    r3 = await provider.fetch_url(url=url, force_refresh=True)
    assert r3["cache"] == "bypassed"
    assert len(fake_firecrawl.scrape_calls) == 2


@pytest.mark.asyncio
async def test_fetch_url_maps_401_to_provider_auth_error(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    from free_web_search_mcp.providers.base import ProviderAuthError
    url = "https://example.com/docs"
    fake_firecrawl.set_scrape_error(url, FirecrawlAuthError("401 Unauthorized"))
    with pytest.raises(ProviderAuthError):
        await provider.fetch_url(url=url, force_refresh=False)


@pytest.mark.asyncio
async def test_fetch_url_empty_body_sets_guidance_note(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    url = "https://example.com/empty"
    fake_firecrawl.set_scrape(url, {
        "markdown": "", "markdown_chars": 0, "markdown_truncated_chars": 0,
        "truncated": False, "title": "Empty", "description": "no body",
        "language": "en", "source_url": url, "final_url": url,
        "status_code": 200,
        "metadata": {"title": "Empty", "description": "no body"},
        "credits_used_this_call": 1,
    })
    resp = await provider.fetch_url(url=url, force_refresh=False)
    assert resp["markdown"] == ""
    assert resp["note"] == EMPTY_SCRAPE_NOTE


# -- Tests: fetch_urls() -----------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_urls_raises_provider_error(provider: FirecrawlProvider) -> None:
    from free_web_search_mcp.providers.base import ProviderError
    with pytest.raises(ProviderError) as exc:
        await provider.fetch_urls(urls=["https://a", "https://b"], force_refresh=False)
    assert "batch URL extract" in str(exc.value)
    assert "tavily" in str(exc.value).lower()


# -- Tests: cache hit returns cached `usage.source='cached'` -----------------


@pytest.mark.asyncio
async def test_search_cache_hit_marks_usage_source_cached(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    fake_firecrawl.set_search("rust", _seed_search_payload("rust", credits=4))
    await provider.search(query="rust", max_results=5, force_refresh=False)
    r2 = await provider.search(query="rust", max_results=5, force_refresh=False)
    assert r2["cache"] == "hit"
    assert r2["usage"]["source"] == "cached"


@pytest.mark.asyncio
async def test_fetch_url_cache_hit_marks_usage_source_cached(
    provider: FirecrawlProvider, fake_firecrawl: FakeFirecrawl,
) -> None:
    url = "https://example.com/docs"
    fake_firecrawl.set_scrape(url, _seed_scrape_payload(url, credits=1))
    await provider.fetch_url(url=url, force_refresh=False)
    r2 = await provider.fetch_url(url=url, force_refresh=False)
    assert r2["cache"] == "hit"
    assert r2["usage"]["source"] == "cached"
