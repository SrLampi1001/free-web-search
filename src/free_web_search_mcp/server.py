"""FastMCP server definition: lifespan, INSTRUCTIONS, /health, and the six tool
bodies that wire the orchestrator's facade to MCP.

Per SPEC §2 / §10: the orchestrator is dual-era (FastMCP 4 default) and serves
both Streamable HTTP and stdio depending on `FWS_TRANSPORT`. Application state
(`settings`, `cache`, `rate_limiters`, `budget`, `http_client`, `providers`)
is owned by the FastMCP `lifespan` context. Phase D adds the six tool bodies;
Phase E replaces the prompt / resource stubs with SPEC §5 / §4 implementations.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal
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


# --- helpers ---------------------------------------------------------------

EMPTY_QUERY_NOTE = "no query, nothing was searched"
EMPTY_RESULT_NOTE = (
    "<empty: rephrase once as a canonical topic name, "
    "or fall back to your own knowledge>"
)

_PROVIDER_DISPLAY_NAMES = {
    "tavily": "Tavily",
    "firecrawl": "Firecrawl",
    "duckduckgo": "DuckDuckGo",
}


def _clamp_count(n: int, lo: int = 1, hi: int = 10) -> int:
    """Clamp `count` / `max_results` to the SPEC §3 [1, 10] window."""
    try:
        return max(lo, min(hi, int(n)))
    except (TypeError, ValueError):
        return 5


def _is_http_url(url: str) -> bool:
    """Accept http(s) only; reject file://, ftp://, javascript:, data:, etc."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _provider_name_for_error(err: BaseException) -> str:
    """Best-effort: which provider raised `err`. Inspects `str(err)`."""
    msg = str(err).lower()
    for needle, display in _PROVIDER_DISPLAY_NAMES.items():
        if needle in msg:
            return display
    return "Provider"


def _translate_error(err: BaseException) -> ToolError:
    """Map a facade/chain exception to the SPEC §14 `ToolError` envelope."""
    if isinstance(err, ChainExhaustedError):
        last = err.fallthroughs[-1] if err.fallthroughs else {}
        return ToolError(
            "All providers refused this call. Check `providers://status` and the "
            "response's `fallthroughs[]` for per-provider reasons. "
            f"Last fallthrough: {last}"
        )
    if isinstance(err, NoProviderError):
        return ToolError(
            "No provider is configured for this tool. Set TAVILY_API_KEY and/or "
            "FIRECRAWL_API_KEY in .env, and ensure FWS_<PROVIDER>_ENABLED=1."
        )
    if isinstance(err, ProviderAuthError):
        name = _provider_name_for_error(err)
        return ToolError(
            f"{name} rejected the API key. Check the relevant env var "
            "(TAVILY_API_KEY or FIRECRAWL_API_KEY) and ensure the key has not "
            "been revoked."
        )
    if isinstance(err, ProviderQuotaError):
        return ToolError(
            "Paid-provider quota exhausted. The orchestrator's chain may have "
            "already fallen through to the other paid provider; check "
            "`provider_used` and `fallthroughs[]` in the previous response. "
            "Otherwise, free tools remain available: entity_search (DDG) and "
            "the free side of the budget."
        )
    if isinstance(err, ProviderRateLimitError):
        return ToolError(
            "Provider is rate-limiting. Wait a few seconds and retry, or use a "
            "free tool (entity_search)."
        )
    if isinstance(err, ProviderUnavailableError):
        return ToolError(
            "Upstream transport error from the provider; the chain did NOT fall "
            "through on transport errors to keep your budget signal honest. "
            "Retry, or use a free tool."
        )
    if isinstance(err, ProviderError):
        return ToolError(f"Provider error: {err}")
    return ToolError(str(err))


def _empty_search_response() -> dict[str, Any]:
    """Degenerate response for an empty query (SPEC §3 'no query' branch)."""
    return {
        "query": "",
        "provider_used": "",
        "fallthroughs": [],
        "results": [],
        "note": EMPTY_QUERY_NOTE,
    }


def _normalise_search_response(response: dict[str, Any]) -> dict[str, Any]:
    """Override `note` with the standard guidance when the adapter returned
    both empty results and empty note."""
    if not response.get("note") and not response.get("results"):
        response["note"] = EMPTY_RESULT_NOTE
    return response


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
        await _hydrate_firecrawl_budget(providers, budget)
        yield {
            "settings": settings,
            "cache": cache,
            "rate_limiters": rate_limiters,
            "budget": budget,
            "http_client": http_client,
            "providers": providers,
        }


async def _hydrate_firecrawl_budget(
    providers: dict, budget: "Budget"
) -> None:
    """SPEC §8 startup probe: call Firecrawl `/v2/team/credit-usage` once.

    Hydrates the local `CreditTracker` so `credit_status()` and `providers://budget`
    surface real numbers from the first request, not `null`. Failures are recorded
    on `probe_note` and the first real call proceeds without a pre-check.

    Tavily has no live remaining-credit endpoint (`CallCounter` is local-only);
    DDG is free. This is Firecrawl-only.
    """
    fc = providers.get(ProviderName.FIRECRAWL)
    if fc is None:
        return
    try:
        snapshot = await fc.credit_usage()
        budget.firecrawl_tracker.hydrate(snapshot)
        log.info(
            "firecrawl startup probe ok: remaining=%s plan=%s resets=%s",
            budget.firecrawl_tracker.remaining_credits,
            budget.firecrawl_tracker.plan_credits,
            budget.firecrawl_tracker.billing_period_end,
        )
    except ProviderAuthError as e:
        log.warning("firecrawl startup probe rejected: %s", e)
        budget.firecrawl_tracker.mark_probe_failed("auth rejected at startup")
    except ProviderQuotaError as e:
        log.warning("firecrawl startup probe: %s", e)
        budget.firecrawl_tracker.mark_probe_failed("out of credits at startup")
    except ProviderUnavailableError as e:
        log.warning("firecrawl startup probe transport error: %s", e)
        budget.firecrawl_tracker.mark_probe_failed(f"transport error: {e}")
    except Exception as e:  # defensive: probe must never break startup
        log.warning("firecrawl startup probe failed: %s", e)
        budget.firecrawl_tracker.mark_probe_failed(str(e))


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


# --- Tool bodies: SPEC §3 implementations. ---------------------------------


@mcp.tool(
    name="entity_search",
    description=(
        "Free entity-shaped web search via the DuckDuckGo Instant Answer API. "
        "Reach for this first for canonical names: people, places, products, "
        "standards, programming languages, calculations. Returns short "
        "snippets and no page bodies. Long-tail or question-shaped queries "
        "often return an empty response — that's a `200` with `results: []` "
        "and a guidance `note`, NOT an error; rephrase once as a canonical "
        "topic name, then fall back to your own knowledge. This tool never "
        "falls through to another provider (DDG has no Auth, Quota, or "
        "RateLimit path that warrants a fallback in this orchestrator)."
    ),
)
async def entity_search(
    query: str = Field(
        description=(
            "Entity-shaped search query — people, places, products, standards, "
            "programming languages, calculations. Canonical names work best."
        ),
        min_length=1,
    ),
    count: int = Field(
        default=5,
        description="Max results to return. Clamped to [1, 10].",
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cache and hit DuckDuckGo upstream.",
    ),
    ctx: Context | None = None,
) -> dict:
    """SPEC §3.1 — DuckDuckGo entity-shaped search."""
    state = _state_from(ctx)
    n = _clamp_count(count)
    echoed = (query or "").strip()
    if not echoed:
        return _empty_search_response()

    try:
        response = await facade.orchestrate(
            intent="entity",
            providers=state["providers"],
            budget=state["budget"],
            op="search",
            query=echoed,
            max_results=n,
            force_refresh=force_refresh,
        )
    except (
        ChainExhaustedError,
        NoProviderError,
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderError,
    ) as e:
        raise _translate_error(e) from e

    return _normalise_search_response(response)


@mcp.tool(
    name="pro_search",
    description=(
        "Question-shaped general web search via Tavily. Reach for this when "
        "the prompt is a question, a long-tail lookup, or anything that wants "
        "LLM-ready snippets (not page bodies). Tavily charges credits per call "
        "(signup tier ~1,000/month; `depth=\"advanced\"` = 2 credits, "
        "`depth=\"basic\"` = 1). Every response includes "
        "`usage.calls_this_session` so the model can self-budget. On `Auth`, "
        "`Quota`, or `RateLimit` errors only, the orchestrator falls through "
        "to `deep_search` (Firecrawl) — empty results DO NOT trigger "
        "fall-through; rephrase and retry. Per-result `raw_content` is "
        "capped at ~8k chars; the cut is visible via `truncated: true`. The "
        "`firecrawl_*` enum is reserved for the `deep_search` tool; do not "
        "call this tool expecting markdown page bodies."
    ),
)
async def pro_search(
    query: str = Field(
        description=(
            "Question-shaped or long-tail web search query. Rephrase as a "
            "specific topic name with concrete terms."
        ),
        min_length=1,
    ),
    max_results: int = Field(
        default=5,
        description="Max results to return. Clamped to [1, 10].",
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cache and hit Tavily upstream.",
    ),
    depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description='Search depth: "basic" (1 credit) or "advanced" (2 credits).',
    ),
    ctx: Context | None = None,
) -> dict:
    """SPEC §3.2 — Tavily general web search."""
    state = _state_from(ctx)
    n = _clamp_count(max_results)
    depth_n = depth if depth in ("basic", "advanced") else "basic"
    echoed = (query or "").strip()
    if not echoed:
        return _empty_search_response()

    try:
        response = await facade.orchestrate(
            intent="general",
            providers=state["providers"],
            budget=state["budget"],
            op="search",
            query=echoed,
            max_results=n,
            force_refresh=force_refresh,
            depth=depth_n,
        )
    except (
        ChainExhaustedError,
        NoProviderError,
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderError,
    ) as e:
        raise _translate_error(e) from e

    return _normalise_search_response(response)


@mcp.tool(
    name="deep_search",
    description=(
        "Deep web search via Firecrawl. Reach for this when you need the "
        "**full page contents** as clean markdown — not just snippets — or "
        "when the prompt is complex enough that snippets won't cut it. Costs "
        "Firecrawl credits (free tier 1,000/month; a typical call uses 2–7 "
        "credits). Every response includes a `usage.remaining_credits` "
        "snapshot — switch to `pro_search` once it drops under ~50. Set "
        "`include_markdown=false` to skip per-result scraping (saves "
        "credits; you only get the description). On `Auth`, `Quota`, or "
        "`RateLimit` errors only, falls through to `pro_search` (Tavily). "
        "Per-result markdown is capped at ~8k chars; the `markdown_chars` / "
        "`markdown_truncated_chars` fields tell you how much was cut."
    ),
)
async def deep_search(
    query: str = Field(
        description=(
            "Deep web search query — complex enough that snippets won't cut "
            "it, or a request that needs full page bodies."
        ),
        min_length=1,
    ),
    max_results: int = Field(
        default=5,
        description="Max results to return. Clamped to [1, 10].",
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cache and hit Firecrawl upstream.",
    ),
    include_markdown: bool = Field(
        default=True,
        description=(
            "Set false to skip per-result scraping (saves credits; you only "
            "get the description)."
        ),
    ),
    ctx: Context | None = None,
) -> dict:
    """SPEC §3.3 — Firecrawl deep web search."""
    state = _state_from(ctx)
    n = _clamp_count(max_results)
    echoed = (query or "").strip()
    if not echoed:
        return _empty_search_response()

    try:
        response = await facade.orchestrate(
            intent="deep",
            providers=state["providers"],
            budget=state["budget"],
            op="search",
            query=echoed,
            max_results=n,
            force_refresh=force_refresh,
            include_markdown=include_markdown,
        )
    except (
        ChainExhaustedError,
        NoProviderError,
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderError,
    ) as e:
        raise _translate_error(e) from e

    return _normalise_search_response(response)


@mcp.tool(
    name="scrape_url",
    description=(
        "Scrape a single known URL via Firecrawl. Reach for this after a "
        "prior `pro_search` or `deep_search` pointed at the page, or when "
        "the user already gave you the URL. Costs 1+ credits. Returns the "
        "page body as clean markdown plus `metadata.title`, "
        "`metadata.description`, `metadata.language`, `metadata.url` "
        "(post-redirect). This tool does NOT fall through; there is no "
        "parallel single-URL extraction service in this set. If Firecrawl "
        "refuses the call, the model falls back to its own knowledge or "
        "summarises from the search-result snippet it already has."
    ),
)
async def scrape_url(
    url: str = Field(
        description="http(s) URL to scrape.",
        min_length=1,
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cache and hit Firecrawl upstream.",
    ),
    ctx: Context | None = None,
) -> dict:
    """SPEC §3.4 — Firecrawl single-URL scrape."""
    state = _state_from(ctx)
    echoed = (url or "").strip()
    if not _is_http_url(echoed):
        raise ToolError(
            f"scrape_url requires an http(s) URL; got {url!r}. "
            "Refusing to forward non-http(s) targets to Firecrawl."
        )

    try:
        response = await facade.orchestrate(
            intent="scrape",
            providers=state["providers"],
            budget=state["budget"],
            op="fetch_url",
            url=echoed,
            force_refresh=force_refresh,
        )
    except (
        ChainExhaustedError,
        NoProviderError,
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderError,
    ) as e:
        raise _translate_error(e) from e

    return response


@mcp.tool(
    name="extract_urls",
    description=(
        "Batch extract one or more URLs via Tavily (1–20 URLs in one call). "
        "Per-URL `raw_content` is capped at ~8k chars; truncation is visible "
        "via `truncated: true`. Failed URLs land in a separate "
        "`failed_results` array (an HTTP 200 can have entries in both "
        "`results` and `failed_results`). This tool does NOT fall through — "
        "single-URL extraction is Firecrawl's lane (`scrape_url`); batch "
        "extraction is Tavily's lane; there is no provider-to-provider swap "
        "on failure."
    ),
)
async def extract_urls(
    urls: list[str] = Field(
        description="1–20 http(s) URLs to extract in one batch.",
        min_length=1,
        max_length=20,
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cache and hit Tavily upstream.",
    ),
    ctx: Context | None = None,
) -> dict:
    """SPEC §3.5 — Tavily batch URL extraction."""
    state = _state_from(ctx)
    if not isinstance(urls, list) or not urls:
        raise ToolError(
            "extract_urls requires a non-empty list of URLs."
        )
    if len(urls) > 20:
        raise ToolError(
            f"extract_urls accepts 1–20 URLs; got {len(urls)}."
        )
    cleaned: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            raise ToolError(
                f"extract_urls URL must be a string; got {type(u).__name__}."
            )
        s = u.strip()
        if not _is_http_url(s):
            raise ToolError(
                f"extract_urls requires an http(s) URL; got {u!r}."
            )
        cleaned.append(s)

    try:
        response = await facade.orchestrate(
            intent="extract",
            providers=state["providers"],
            budget=state["budget"],
            op="fetch_urls",
            urls=cleaned,
            force_refresh=force_refresh,
        )
    except (
        ChainExhaustedError,
        NoProviderError,
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderError,
    ) as e:
        raise _translate_error(e) from e

    return response


@mcp.tool(
    name="credit_status",
    description=(
        "Merged view of all three providers' budgets plus a routing "
        "recommendation computed from the rules in §6. Does NOT burn credits "
        "(Firecrawl's live probe is a metadata endpoint; Tavily's snapshot is "
        "in-process; DDG is free). Call this before a batch of expensive calls."
    ),
)
async def credit_status(ctx: Context | None = None) -> dict:
    """SPEC §3.6 — merged budget view across all three providers."""
    state = _state_from(ctx)
    return await facade.orchestrate(
        intent="status",
        providers=state["providers"],
        budget=state["budget"],
        op="status",
    )


def _state_from(ctx: Context | None) -> dict:
    if ctx is None or ctx.request_context is None:
        raise RuntimeError("tool called without a request context")
    span = ctx.request_context.lifespan_context
    if not span:
        raise RuntimeError("server lifespan did not initialise state")
    return span
