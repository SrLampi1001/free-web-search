"""FastMCP server definition: lifespan, INSTRUCTIONS, /health, and the six tool
bodies that wire the orchestrator's facade to MCP.

Per SPEC §2 / §10: the orchestrator is dual-era (FastMCP 4 default) and serves
both Streamable HTTP and stdio depending on `FWS_TRANSPORT`. Application state
(`settings`, `cache`, `rate_limiters`, `budget`, `http_client`, `providers`)
is owned by the FastMCP `lifespan` context. Phase D filled in the six tool
bodies; Phase E replaces the prompt / resource stubs with SPEC §5 / §4.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .budget import Budget
from .cache import DiskCache
from .providers import ProviderContext, ProviderName, build_all, facade
from .providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderName as _ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from .providers.chain import ChainExhaustedError
from .providers.router import NoProviderError
from .prompts import register_prompts
from .rate_limit import MinIntervalLimiter
from .resources import register_resources
from .settings import Settings, configure_logging

log = logging.getLogger(__name__)


INSTRUCTIONS = (
    "Six tools: entity_search (free DuckDuckGo entity snippets), "
    "pro_search (Tavily general web search), deep_search (Firecrawl deep "
    "markdown), scrape_url (Firecrawl single-URL), extract_urls (Tavily "
    "batch extraction), credit_status (merged budget view). "
    "Three prompts: route_query, research_brief, audit_budget. "
    "Four resources: providers://status, providers://budget, providers://schema, "
    "cache://stats. Routing is fixed per SPEC §6: Auth/Quota/RateLimit failures "
    "fall through to the other paid provider of the same shape; empty results "
    "do not engage the chain; DDG never falls through. No authentication on the "
    "server; loopback bind by design."
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncGenerator[dict, None]:
    """One-shot startup: read settings, build state, register providers.

    State (read-only view of what lives inside `lifespan_context`):

    - `settings: Settings`
    - `cache: DiskCache` (single instance, namespaced per-provider)
    - `rate_limiters: dict[ProviderName, MinIntervalLimiter]`
    - `budget: Budget` (aggregates Tavily counter + Firecrawl tracker + DDG flag)
    - `http_client: httpx.AsyncClient` (single shared client)
    - `providers: dict[ProviderName, Provider]`
    """
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    log.info(
        "starting: transport=%s port=%d path=%s cache=%s",
        settings.transport,
        settings.port,
        settings.path,
        settings.cache_dir,
    )

    cache = DiskCache(settings.cache_dir, ttl_seconds=86400)

    rate_limiters: dict[ProviderName, MinIntervalLimiter] = {}
    if settings.ddg_enabled:
        rate_limiters[ProviderName.DUCKDUCKGO] = MinIntervalLimiter(settings.ddg_min_interval_ms)
    if settings.tavily_enabled:
        rate_limiters[ProviderName.TAVILY] = MinIntervalLimiter(settings.tavily_min_interval_ms)
    if settings.firecrawl_enabled:
        rate_limiters[ProviderName.FIRECRAWL] = MinIntervalLimiter(settings.firecrawl_min_interval_ms)

    budget = Budget(
        tavily_configured=settings.tavily_enabled,
        firecrawl_configured=settings.firecrawl_enabled,
        ddg_configured=settings.ddg_enabled,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": "free-web-search-mcp/0.1 (+https://github.com/)"},
        follow_redirects=True,
        timeout=settings.http_timeout_s,
    ) as http_client:
        ctx = ProviderContext(
            settings=settings,
            cache=cache,
            rate_limiters=rate_limiters,
            budget=budget,
            http_client=http_client,
        )
        providers = build_all(ctx)
        yield {
            "settings": settings,
            "cache": cache,
            "rate_limiters": rate_limiters,
            "budget": budget,
            "http_client": http_client,
            "providers": providers,
        }


mcp = FastMCP(
    name="free-web-search-mcp",
    instructions=INSTRUCTIONS,
    cache_ttl=300,        # tool/resource/prompt lists rarely change; safe to share publicly
    cache_scope="public",
    lifespan=lifespan,
)

register_prompts(mcp)
register_resources(mcp)


@mcp.custom_route(path="/health", methods=["GET"], name="health")
async def health(_: Request) -> Response:
    """Liveness probe; returns 200 OK regardless of providers or cache state."""
    return JSONResponse({"status": "ok"})


# --- Tool bodies: Phase D replaces each of these with the SPEC §3 implementation.


@mcp.tool(
    name="entity_search",
    description=(
        "Phase A stub. Free entity-shaped web search via the DuckDuckGo Instant Answer API. "
        "Phase D will replace this with the SPEC §3.1 implementation."
    ),
)
async def entity_search(
    query: str,
    count: int = 5,
    force_refresh: bool = False,
    ctx: Context | None = None,
) -> dict:
    raise NotImplementedError("Phase D (D-tools sub-agent) will implement entity_search per SPEC §3.1.")


@mcp.tool(
    name="pro_search",
    description=(
        "Phase A stub. General web search via Tavily. "
        "Phase D will replace this with the SPEC §3.2 implementation."
    ),
)
async def pro_search(
    query: str,
    max_results: int = 5,
    force_refresh: bool = False,
    depth: str = "basic",
    ctx: Context | None = None,
) -> dict:
    raise NotImplementedError("Phase D will implement pro_search per SPEC §3.2.")


@mcp.tool(
    name="deep_search",
    description=(
        "Phase A stub. Deep web search via Firecrawl. "
        "Phase D will replace this with the SPEC §3.3 implementation."
    ),
)
async def deep_search(
    query: str,
    max_results: int = 5,
    force_refresh: bool = False,
    include_markdown: bool = True,
    ctx: Context | None = None,
) -> dict:
    raise NotImplementedError("Phase D will implement deep_search per SPEC §3.3.")


@mcp.tool(
    name="scrape_url",
    description=(
        "Phase A stub. Single-URL scrape via Firecrawl. "
        "Phase D will replace this with the SPEC §3.4 implementation."
    ),
)
async def scrape_url(url: str, force_refresh: bool = False, ctx: Context | None = None) -> dict:
    raise NotImplementedError("Phase D will implement scrape_url per SPEC §3.4.")


@mcp.tool(
    name="extract_urls",
    description=(
        "Phase A stub. Batch URL extract via Tavily. "
        "Phase D will replace this with the SPEC §3.5 implementation."
    ),
)
async def extract_urls(urls: list[str], force_refresh: bool = False, ctx: Context | None = None) -> dict:
    raise NotImplementedError("Phase D will implement extract_urls per SPEC §3.5.")


@mcp.tool(
    name="credit_status",
    description=(
        "Phase A stub. Merged budget view across all three providers. "
        "Phase D will replace this with the SPEC §3.6 implementation."
    ),
)
async def credit_status(ctx: Context | None = None) -> dict:
    raise NotImplementedError("Phase D will implement credit_status per SPEC §3.6.")


def _state_from(ctx: Context | None) -> dict:
    if ctx is None or ctx.request_context is None:
        raise RuntimeError("tool called without a request context")
    span = ctx.request_context.lifespan_context
    if not span:
        raise RuntimeError("server lifespan did not initialise state")
    return span
