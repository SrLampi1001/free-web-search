"""Hermetic tests for the four SPEC §4 resources in `resources.register_resources`.

Status / budget / cache resources read the lifespan state (stubbed via
SimpleNamespace, same pattern as `test_server.py`). The schema resource
runs against the real `server.mcp` so all six tools + three prompts + four
resources are present per SPEC §4.3. Resource bodies live at module level in
`resources.py` so tests call them directly with a stubbed ctx.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from free_web_search_mcp.budget import Budget
from free_web_search_mcp.cache import DiskCache
from free_web_search_mcp.providers.base import (
    ProviderABC,
    ProviderName,
)
from free_web_search_mcp.resources import (
    register_resources,
    providers_status,
    providers_budget,
    providers_schema,
    cache_stats,
)
from free_web_search_mcp.server import mcp


# --- helpers ---------------------------------------------------------------


class FakeProvider:
    """Adapter stub satisfying `ProviderABC` structurally (mirrors `test_chain.py`)."""

    def __init__(
        self,
        name: ProviderName,
        *,
        returns: dict | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.configured = True
        self._returns = returns if returns is not None else {}
        self._raises = raises
        self._supports = {"search": True, "fetch_url": True, "fetch_urls": True}

    def supports(self, operation: str) -> bool:
        return self._supports.get(operation, False)

    async def search(self, **_kwargs) -> dict:
        return self._call()

    async def fetch_url(self, **_kwargs) -> dict:
        return self._call()

    async def fetch_urls(self, **_kwargs) -> dict:
        return self._call()

    def _call(self) -> dict:
        if self._raises is not None:
            raise self._raises
        return self._returns


def _make_ctx(
    *,
    providers: dict[ProviderName, ProviderABC] | None = None,
    budget: Budget | None = None,
    cache: DiskCache | None = None,
    extras: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build a stub `Context` whose `request_context.lifespan_context`
    matches the real lifespan shape; tests pass this as `ctx=` to a
    resource function."""
    state: dict[str, Any] = {
        "settings": None,
        "cache": cache or DiskCache(_fresh_tmp_dir(), ttl_seconds=86400),
        "rate_limiters": {},
        "budget": budget or Budget(),
        "http_client": None,
        "providers": providers or {},
    }
    if extras:
        state.update(extras)
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=state)
    )


def _fresh_tmp_dir() -> Path:
    """Build + clean up a tmp Path; we re-create the dir on every call so
    tests never share state."""
    with tempfile.TemporaryDirectory(prefix="fws-res-") as d:
        return Path(d)


# --- tests: list / registration shape -----------------------------------


@pytest.mark.asyncio
async def test_register_resources_binds_four_uris() -> None:
    """`register_resources` exposes the exact four SPEC §4 URIs."""
    fresh = FastMCP("probe-list")
    register_resources(fresh)
    async with Client(fresh) as c:
        resources = await c.list_resources()
        uris = sorted(str(r.uri) for r in resources)
    assert uris == sorted([
        "cache://stats",
        "providers://budget",
        "providers://schema",
        "providers://status",
    ])


@pytest.mark.asyncio
async def test_resources_have_json_mime_and_annotations() -> None:
    """Every resource advertises `application/json` and registers an
    Annotations object. NOTE: FastMCP 4 constructs `mcp.types.Annotations`
    from the decorator dict, which silently drops `readOnlyHint` /
    `idempotentHint` (those fields are absent from the SDK v2 `Annotations`
    model). We still pass the hints per SPEC; the wire round-trip is
    FastMCP's responsibility, not ours."""
    fresh = FastMCP("probe-meta")
    register_resources(fresh)
    async with Client(fresh) as c:
        resources = await c.list_resources()
    for r in resources:
        assert r.mime_type == "application/json", f"{r.uri}"
        # Annotations object exists; we do not assert hint fields because
        # the underlying SDK v2 model exposes only audience/priority/last_modified.
        assert r.annotations is not None
        assert hasattr(r.annotations, "model_dump")


# --- tests: providers://status ------------------------------------------


@pytest.mark.asyncio
async def test_providers_status_top_level_keys() -> None:
    """Each row carries the four SPEC §4.1 keys."""
    tavily = FakeProvider(ProviderName.TAVILY)
    firecrawl = FakeProvider(ProviderName.FIRECRAWL)
    ctx = _make_ctx(
        providers={ProviderName.TAVILY: tavily, ProviderName.FIRECRAWL: firecrawl}
    )
    payload = await providers_status(ctx)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"tavily", "firecrawl", "duckduckgo"}
    for name in payload:
        assert set(payload[name].keys()) == {
            "configured",
            "last_call_unix",
            "last_error",
            "consecutive_errors",
        }


@pytest.mark.asyncio
async def test_providers_status_marks_configured_only_for_active_providers() -> None:
    """`configured` reflects lifespan membership, not env-var presence."""
    tavily = FakeProvider(ProviderName.TAVILY)
    firecrawl = FakeProvider(ProviderName.FIRECRAWL)
    ctx = _make_ctx(
        providers={ProviderName.TAVILY: tavily, ProviderName.FIRECRAWL: firecrawl}
    )
    payload = await providers_status(ctx)
    assert payload["tavily"]["configured"] is True
    assert payload["firecrawl"]["configured"] is True
    assert payload["duckduckgo"]["configured"] is False


@pytest.mark.asyncio
async def test_providers_status_surfaces_call_error_counters() -> None:
    """When lifespan tracks `last_call_unix` / `last_error` /
    `consecutive_errors`, surface them verbatim."""
    tavily = FakeProvider(ProviderName.TAVILY)
    firecrawl = FakeProvider(ProviderName.FIRECRAWL)
    extras = {
        "last_call_unix": {"tavily": 1730000000.0, "firecrawl": 1730000001.5},
        "last_error": {"firecrawl": "FirecrawlCreditsError at 1730000001.5"},
        "consecutive_errors": {"firecrawl": 1},
    }
    ctx = _make_ctx(
        providers={ProviderName.TAVILY: tavily, ProviderName.FIRECRAWL: firecrawl},
        extras=extras,
    )
    payload = await providers_status(ctx)

    assert payload["tavily"]["last_call_unix"] == 1730000000.0
    assert payload["tavily"]["last_error"] == ""
    assert payload["tavily"]["consecutive_errors"] == 0
    assert payload["firecrawl"]["last_call_unix"] == 1730000001.5
    assert payload["firecrawl"]["last_error"] == "FirecrawlCreditsError at 1730000001.5"
    assert payload["firecrawl"]["consecutive_errors"] == 1


@pytest.mark.asyncio
async def test_providers_status_defaults_when_no_counters_tracked() -> None:
    """Without optional `last_*` keys in lifespan state, defaults to 0.0 / '' / 0."""
    tavily = FakeProvider(ProviderName.TAVILY)
    ctx = _make_ctx(providers={ProviderName.TAVILY: tavily})
    payload = await providers_status(ctx)

    assert payload["tavily"]["last_call_unix"] == 0.0
    assert payload["tavily"]["last_error"] == ""
    assert payload["tavily"]["consecutive_errors"] == 0


# --- tests: providers://budget ------------------------------------------


@pytest.mark.asyncio
async def test_providers_budget_top_level_keys_and_called_at() -> None:
    budget = Budget(tavily_configured=True, firecrawl_configured=True, ddg_configured=True)
    budget.tavily_counter.charge(by=4)
    budget.firecrawl_tracker.hydrate({"remaining_credits": 900, "plan_credits": 1000})
    ctx = _make_ctx(budget=budget)

    payload = await providers_budget(ctx)
    assert isinstance(payload, dict)
    assert "providers" in payload
    assert "recommendation" in payload
    assert "called_at_unix" in payload
    assert isinstance(payload["called_at_unix"], float)


@pytest.mark.asyncio
async def test_providers_budget_lists_all_three_providers() -> None:
    budget = Budget(tavily_configured=True, firecrawl_configured=True, ddg_configured=True)
    budget.tavily_counter.charge(by=2)
    budget.firecrawl_tracker.hydrate({"remaining_credits": 800, "plan_credits": 1000})
    ctx = _make_ctx(budget=budget)

    payload = await providers_budget(ctx)
    assert set(payload["providers"].keys()) == {"tavily", "firecrawl", "duckduckgo"}
    assert payload["providers"]["tavily"]["calls_this_session"] == 2
    assert payload["providers"]["firecrawl"]["remaining_credits"] == 800
    assert payload["providers"]["duckduckgo"]["configured"] is True


@pytest.mark.asyncio
async def test_providers_budget_with_no_keys() -> None:
    """SPEC §14: missing keys must degrade gracefully to `configured: false`."""
    ctx = _make_ctx(budget=Budget())
    payload = await providers_budget(ctx)

    assert payload["providers"]["tavily"]["configured"] is False
    assert payload["providers"]["firecrawl"]["configured"] is False
    assert payload["providers"]["duckduckgo"]["configured"] is True


# --- tests: providers://schema -----------------------------------------


@pytest.mark.asyncio
async def test_providers_schema_top_level_keys() -> None:
    """Exercise against the real server `mcp` so all six tools + three
    prompts + four resources are present (per SPEC §4.3)."""
    payload = await providers_schema(mcp=mcp)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"tools", "resources", "prompts"}
    assert isinstance(payload["tools"], list)
    assert isinstance(payload["resources"], list)
    assert isinstance(payload["prompts"], list)


@pytest.mark.asyncio
async def test_providers_schema_lists_the_six_tools() -> None:
    payload = await providers_schema(mcp=mcp)

    tool_names = sorted(t["name"] for t in payload["tools"])
    assert tool_names == sorted([
        "entity_search",
        "pro_search",
        "deep_search",
        "scrape_url",
        "extract_urls",
        "credit_status",
    ])

    for t in payload["tools"]:
        assert "input_schema" in t
        assert "description" in t
        assert "output_schema" in t
        assert isinstance(t["input_schema"], dict)
        assert t["input_schema"].get("type") == "object"


@pytest.mark.asyncio
async def test_providers_schema_lists_three_prompts() -> None:
    payload = await providers_schema(mcp=mcp)

    prompt_names = sorted(p["name"] for p in payload["prompts"])
    assert prompt_names == sorted(["route_query", "research_brief", "audit_budget"])

    route = next(p for p in payload["prompts"] if p["name"] == "route_query")
    arg_names = [a["name"] for a in route["arguments"]]
    assert "topic" in arg_names


@pytest.mark.asyncio
async def test_providers_schema_lists_four_resources() -> None:
    payload = await providers_schema(mcp=mcp)
    uris = sorted(r["uri"] for r in payload["resources"])
    assert uris == sorted([
        "providers://status",
        "providers://budget",
        "providers://schema",
        "cache://stats",
    ])


@pytest.mark.asyncio
async def test_providers_schema_through_client_matches_function_shape() -> None:
    """End-to-end: read the resource via a real FastMCP client and verify
    the JSON payload matches the direct function call shape."""
    async with Client(mcp) as c:
        result = await c.read_resource("providers://schema")
    payload_via_client = json.loads(result[0].text)

    payload_direct = await providers_schema(mcp=mcp)

    # Top-level keys must line up (the client serialises keys alphabetically
    # but contents are identical).
    assert set(payload_via_client.keys()) == set(payload_direct.keys())
    assert set(t["name"] for t in payload_via_client["tools"]) == set(
        t["name"] for t in payload_direct["tools"]
    )


# --- tests: cache://stats ----------------------------------------------


@pytest.mark.asyncio
async def test_cache_stats_returns_totals_block() -> None:
    """SPEC §4.4 shape: per-namespace blocks plus an aggregated `totals`."""
    with tempfile.TemporaryDirectory(prefix="fws-cache-stats-") as d:
        cache = DiskCache(Path(d), ttl_seconds=86400)
        cache.set("tavily", "rust", "search", {"results": []})
        cache.set("firecrawl", "rust", "search", {"results": []})
        cache.set("duckduckgo", "rust", "search", {"results": []})

        ctx = _make_ctx(cache=cache)
        payload = await cache_stats(ctx)

        assert isinstance(payload, dict)
        assert "totals" in payload
        totals = payload["totals"]
        assert set(totals.keys()) == {"hits", "misses", "dir_size_bytes", "last_purge_unix"}
        assert totals["hits"] == 0
        assert totals["misses"] == 0
        assert isinstance(totals["dir_size_bytes"], int)
        assert totals["last_purge_unix"] == 0


@pytest.mark.asyncio
async def test_cache_stats_per_namespace_blocks() -> None:
    with tempfile.TemporaryDirectory(prefix="fws-cache-stats-2-") as d:
        cache = DiskCache(Path(d), ttl_seconds=86400)
        cache.set("tavily", "rust", "search", {"results": [{"title": "T"}]})
        cache.set("firecrawl", "rust", "search", {"results": [{"title": "F"}]})
        cache.set("duckduckgo", "rust", "search", {"results": [{"title": "D"}]})

        ctx = _make_ctx(cache=cache)
        payload = await cache_stats(ctx)

        for name in ("tavily", "firecrawl", "duckduckgo"):
            assert name in payload
            assert set(payload[name].keys()) == {"hits", "misses", "dir_size_bytes"}
            assert payload[name]["dir_size_bytes"] > 0


@pytest.mark.asyncio
async def test_cache_stats_totals_match_sum_of_per_namespace() -> None:
    """`totals.hits` + `totals.misses` must equal the sum across providers."""
    with tempfile.TemporaryDirectory(prefix="fws-cache-stats-3-") as d:
        cache = DiskCache(Path(d), ttl_seconds=86400)
        cache.set("tavily", "a", "search", {"x": 1})
        cache.set("firecrawl", "a", "search", {"x": 2})
        cache.set("duckduckgo", "a", "search", {"x": 3})

        ctx = _make_ctx(cache=cache)
        payload = await cache_stats(ctx)

        per_sum = sum(payload[n]["dir_size_bytes"] for n in ("tavily", "firecrawl", "duckduckgo"))
        assert payload["totals"]["dir_size_bytes"] == per_sum
