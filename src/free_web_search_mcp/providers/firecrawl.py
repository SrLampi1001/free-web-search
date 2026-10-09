"""Firecrawl provider adapter for the orchestrator.

Per SPEC §3.3 / §3.4: the orchestrator's `deep_search` and `scrape_url` tools
route here. Adapts the Firecrawl v2 API to the SPEC response shape, manages
the cache, rate limit, and the Firecrawl credit tracker in `ctx.budget`.
Maps Firecrawl-specific errors to the orchestrator's neutral taxonomy at the
adapter boundary.

The `FirecrawlClient` and `_map_*` helpers are copied verbatim from
`fireclaw-search-mcp::firecrawl.py`. The adapter instantiates the client
with a very large markdown cap so the mappers never truncate; truncation is
reapplied at presentation time from the cached full body so re-querying with
a different `FWS_FIRECRAWL_MAX_MARKDOWN_CHARS` reuses the cached content.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Iterable
from urllib.parse import urlparse

import httpx

from ..credits import ToolBudgetError
from ..normalize import normalize_query
from .base import (
    ProviderAuthError,
    ProviderError,
    ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"
USER_AGENT = "free-web-search-mcp/0.1"
# Cap the mapper's truncation so the cached body is always the full upstream content.
PRESERVE_CHARS = 10_000_000

EMPTY_SEARCH_NOTE = (
    "Firecrawl returned no web results for this query. Rephrase once as a "
    "specific topic name with concrete terms, and if that also comes back "
    "empty, answer from your own knowledge and say so."
)
EMPTY_SCRAPE_NOTE = (
    "Firecrawl returned no markdown body for this URL. The page may be empty, "
    "paywalled, or behind a JS shell. Try a different URL, or summarise "
    "whatever metadata.title and metadata.description carry."
)


# -- Firecrawl-specific exception hierarchy (copied from sibling) ------------


class FirecrawlError(RuntimeError):
    """Raised on a Firecrawl transport or non-2xx HTTP error."""


class FirecrawlAuthError(FirecrawlError):
    """Raised when Firecrawl rejects the API key (HTTP 401)."""


class FirecrawlCreditsError(FirecrawlError):
    """Raised when Firecrawl reports out-of-credits (HTTP 402)."""


class FirecrawlRateLimitError(FirecrawlError):
    """Raised when Firecrawl rate-limits (HTTP 429)."""


# -- HTTP client (copied verbatim from sibling) ------------------------------


class FirecrawlClient:
    """Async HTTP client for the three Firecrawl endpoints this server uses."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        max_markdown_chars: int = 8000,
    ) -> None:
        if not api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set; tools will refuse to call Firecrawl")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_markdown_chars = max_markdown_chars
        self._client: httpx.AsyncClient | None = None

    def attach(self, client: httpx.AsyncClient) -> None:
        """Bind a shared httpx client; lifespan-managed."""
        self._client = client

    async def aclose(self) -> None:
        # No-op; the shared client is owned by the lifespan.
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise FirecrawlError("Firecrawl client not attached to a shared httpx session")
        return self._client

    async def search(
        self,
        query: str,
        limit: int = 5,
        *,
        include_markdown: bool = True,
    ) -> dict[str, Any]:
        """Run Firecrawl /v2/search. `include_markdown=False` returns web entries
        without scrape (saves credits; description/snippet only)."""
        body: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(limit), 10)),
        }
        if include_markdown:
            body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
        payload = await self._post("/search", body)
        return _map_search_response(payload, self._max_markdown_chars)

    async def scrape(self, url: str) -> dict[str, Any]:
        """Run Firecrawl /v2/scrape on a single URL. Returns the mapped response
        with markdown truncated and metadata projected onto the SPECS §3.2 shape."""
        body = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
        payload = await self._post("/scrape", body)
        return _map_scrape_response(payload, self._max_markdown_chars)

    async def credit_usage(self) -> dict[str, Any]:
        """Run Firecrawl /v2/team/credit-usage. Maps to the SPECS §3.3 credits object."""
        payload = await self._get("/team/credit-usage")
        return _map_credit_usage_response(payload)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self._base_url + path,
                json=body,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as e:
            raise FirecrawlError(f"Firecrawl transport error: {e.__class__.__name__}: {e}") from e
        return _parse_response(resp)

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(
                self._base_url + path,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as e:
            raise FirecrawlError(f"Firecrawl transport error: {e.__class__.__name__}: {e}") from e
        return _parse_response(resp)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


# ----------------------------------------------------------------------- I/O --


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """Validate an HTTP response and return the JSON body. Raises typed errors."""
    if resp.status_code == 401:
        raise FirecrawlAuthError("Firecrawl rejected the API key (401 Unauthorized)")
    if resp.status_code == 402:
        # Firecrawl signals out-of-credits with 402 (also `insufficient_credits` in body).
        raise FirecrawlCreditsError("Firecrawl is out of credits for this billing period (402)")
    if resp.status_code == 429:
        raise FirecrawlRateLimitError("Firecrawl rate-limited this key (429 Too Many Requests)")
    if resp.status_code >= 400:
        text = (resp.text or "")[:200]
        raise FirecrawlError(f"Firecrawl upstream HTTP {resp.status_code}: {text!r}")
    try:
        body = resp.json()
    except ValueError as e:
        raise FirecrawlError(f"Firecrawl returned non-JSON body (status {resp.status_code}): {e}") from e
    if isinstance(body, dict) and body.get("success") is False:
        err = body.get("error") or body.get("code") or "unknown"
        if body.get("code") == "insufficient_credits" or "402" in str(err):
            raise FirecrawlCreditsError(f"Firecrawl out of credits: {err}")
        raise FirecrawlError(f"Firecrawl reported failure: {err}")
    return body


# ------------------------------------------------------------------ mappings --


def _map_search_response(payload: dict[str, Any], max_markdown_chars: int) -> dict[str, Any]:
    """Project /v2/search payload onto the SPECS §3.1 shape."""
    data = payload.get("data") or {}
    web = data.get("web") or []
    results: list[dict[str, Any]] = []
    for entry in web:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            # Skip search-engine noise — entries with no resolved URL.
            continue
        markdown = entry.get("markdown") or ""
        markdown_chars = len(markdown)
        truncated = markdown_chars > max_markdown_chars
        if truncated:
            markdown = _truncate_markdown(markdown, max_markdown_chars)
        results.append(
            {
                "title": entry.get("title") or "",
                "url": url,
                "description": entry.get("description") or "",
                "markdown": markdown,
                "markdown_chars": markdown_chars,
                "markdown_truncated_chars": len(markdown) if truncated else markdown_chars,
                "truncated": truncated,
            }
        )
    credits_used = _coerce_int(payload.get("creditsUsed")) or _sum_provider_costs(web)
    return {
        "results": results,
        "credits_used_this_call": credits_used,
        "web_raw_count": len(web),
    }


def _map_scrape_response(payload: dict[str, Any], max_markdown_chars: int) -> dict[str, Any]:
    """Project /v2/scrape payload onto the SPECS §3.2 shape."""
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    raw_markdown = data.get("markdown") or ""
    markdown_chars = len(raw_markdown)
    truncated = markdown_chars > max_markdown_chars
    if truncated:
        markdown = _truncate_markdown(raw_markdown, max_markdown_chars)
    else:
        markdown = raw_markdown
    metadata = data.get("metadata") or {}
    projected_metadata = {
        "title": _first_str(metadata.get("title")),
        "description": _first_str(metadata.get("description")),
        "language": _first_str(metadata.get("language")),
        "source_url": metadata.get("sourceURL") or metadata.get("source_url") or "",
        "url": metadata.get("url") or "",
        "status_code": metadata.get("statusCode") or metadata.get("status_code"),
    }
    credits_used = _coerce_int(payload.get("creditsUsed"))
    if credits_used is None:
        provider_cost = (metadata.get("provider") or {}).get("creditsCost")
        credits_used = _coerce_int(provider_cost) or 1
    return {
        "markdown": markdown,
        "markdown_chars": markdown_chars,
        "markdown_truncated_chars": len(markdown) if truncated else markdown_chars,
        "truncated": truncated,
        "title": projected_metadata["title"],
        "description": projected_metadata["description"],
        "language": projected_metadata["language"],
        "source_url": projected_metadata["source_url"],
        "final_url": projected_metadata["url"],
        "status_code": projected_metadata["status_code"],
        "metadata": projected_metadata,
        "credits_used_this_call": credits_used,
    }


def _map_credit_usage_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Project /v2/team/credit-usage payload onto the SPECS §3.3 credits object."""
    data = payload.get("data") or {}
    return {
        "remaining_credits": _coerce_int(data.get("remainingCredits")),
        "plan_credits": _coerce_int(data.get("planCredits")),
        "billing_period_start": data.get("billingPeriodStart"),
        "billing_period_end": data.get("billingPeriodEnd"),
        "raw": data,
    }


# ----------------------------------------------------------------- helpers --


_TRUNCATION_MARKER = "\n\n[…markdown truncated to fit the context budget; ask for a narrower scope or call scrape_url with a focus…]"


def _truncate_markdown(markdown: str, max_chars: int) -> str:
    """Slice to leave room for the marker, then append it. Total stays <= max_chars."""
    room = max_chars - len(_TRUNCATION_MARKER)
    if room <= 0:
        # Defensive: the configured cap is too small for the marker itself.
        return markdown[:max_chars]
    return markdown[:room] + _TRUNCATION_MARKER


def _sum_provider_costs(web: Iterable[Any]) -> int:
    total = 0
    for entry in web:
        if not isinstance(entry, dict):
            continue
        cost = (entry.get("metadata") or {}).get("provider", {}).get("creditsCost")
        cost = _coerce_int(cost)
        if cost:
            total += cost
    return total


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_str(value: Any) -> str:
    """Metadata `title`/`description`/`language` may come as str or list[str]."""
    if isinstance(value, list):
        return next((str(v) for v in value if v), "")
    if value is None:
        return ""
    return str(value)


# -- Orchestrator adapter ------------------------------------------------------


class FirecrawlProvider:
    """SPEC §3.3 / §3.4 adapter. Single-flight per `ctx.rate_limiters[FIRECRAWL]`."""

    name = ProviderName.FIRECRAWL

    def __init__(self, ctx: "ProviderContext") -> None:  # type: ignore[name-defined]
        self._ctx = ctx
        self.configured = bool(ctx.settings.firecrawl_api_key)
        # Tolerate the empty-key case so `configured == False` is the single source of truth.
        if self.configured:
            self._client = FirecrawlClient(
                api_key=ctx.settings.firecrawl_api_key,
                base_url=ctx.settings.firecrawl_base_url,
                timeout_s=ctx.settings.http_timeout_s,
                max_markdown_chars=PRESERVE_CHARS,
            )
            self._client.attach(ctx.http_client)
        else:
            self._client = None  # type: ignore[assignment]

    def supports(self, operation: str) -> bool:
        return operation in ("search", "fetch_url")

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        force_refresh: bool,
        depth: str | None = None,
        include_markdown: bool = True,
    ) -> dict[str, Any]:
        """SPEC §3.3 shape."""
        self._require_configured()
        echoed = query.strip()
        norm = normalize_query(echoed)
        n = max(1, min(10, int(max_results) or 5))
        cache_key = f"{norm}|md={include_markdown}"

        self._guard_budget()

        if not force_refresh:
            cached = self._ctx.cache.get(self.name.value, cache_key)
            if cached is not None:
                return self._shape_search(echoed, cached.response, cache="hit")

        await self._ctx.rate_limiters[ProviderName.FIRECRAWL].wait()
        mapped = await self._call(self._client.search(echoed, limit=n, include_markdown=include_markdown))
        self._charge_budget(mapped)
        self._ctx.cache.set(self.name.value, cache_key, "search", dict(mapped))
        return self._shape_search(echoed, mapped, cache="bypassed" if force_refresh else "miss")

    async def fetch_url(self, *, url: str, force_refresh: bool) -> dict[str, Any]:
        """SPEC §3.4 shape. Per SPEC §8, no budget guard on single-URL scrape."""
        self._require_configured()
        echoed = url.strip()
        if not _is_http_url(echoed):
            raise ProviderError(
                f"scrape_url requires an http(s) URL; got {url!r}. "
                "Refusing to forward non-http(s) targets to Firecrawl."
            )
        # NOTE: scrape is a known-cost single call — no pre-call guard.
        ns = "firecrawl_scrape"

        if not force_refresh:
            cached = self._ctx.cache.get(ns, echoed)
            if cached is not None:
                return self._shape_scrape(echoed, cached.response, cache="hit")

        await self._ctx.rate_limiters[ProviderName.FIRECRAWL].wait()
        mapped = await self._call(self._client.scrape(echoed))
        self._charge_budget(mapped)
        self._ctx.cache.set(ns, echoed, "scrape", dict(mapped))
        return self._shape_scrape(echoed, mapped, cache="bypassed" if force_refresh else "miss")

    async def fetch_urls(self, *, urls: list[str], force_refresh: bool) -> dict[str, Any]:
        """Firecrawl has no batch-equivalent; refuse with a clear error."""
        raise ProviderError(
            "Firecrawl does not support batch URL extract; use Tavily's extract_urls"
        )

    # -- internals -----------------------------------------------------------

    def _require_configured(self) -> None:
        if not self.configured:
            raise ProviderError(
                "FIRECRAWL_API_KEY is not set; Firecrawl is not registered. "
                "Set FIRECRAWL_API_KEY in .env to enable Firecrawl tools."
            )

    def _guard_budget(self) -> None:
        try:
            self._ctx.budget.firecrawl_tracker.guard(self._ctx.settings.firecrawl_min_credits)
        except ToolBudgetError as e:
            raise ProviderQuotaError(str(e)) from e

    def _charge_budget(self, mapped: dict[str, Any]) -> None:
        credits = _coerce_int(mapped.get("credits_used_this_call")) or 0
        self._ctx.budget.firecrawl_tracker.charge(credits)

    async def _call(self, coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Run a Firecrawl call and map its errors to the orchestrator taxonomy."""
        try:
            return await coro
        except FirecrawlAuthError as e:
            raise ProviderAuthError(str(e)) from e
        except FirecrawlCreditsError as e:
            raise ProviderQuotaError(str(e)) from e
        except FirecrawlRateLimitError as e:
            raise ProviderRateLimitError(str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderUnavailableError(
                f"Firecrawl upstream transport error: {e.__class__.__name__}: {e}"
            ) from e
        except FirecrawlError as e:
            raise ProviderError(str(e)) from e

    def _shape_search(self, echoed: str, mapped: dict[str, Any], *, cache: str) -> dict[str, Any]:
        cap = self._ctx.settings.firecrawl_max_markdown_chars
        per_result = [_reapply_truncation(r, cap) for r in mapped.get("results", [])]
        per_result = per_result[: max(1, min(10, len(per_result) or 1))]
        credits = int(mapped.get("credits_used_this_call") or 0)
        return {
            "query": echoed,
            "provider_used": "firecrawl",
            "fallthroughs": [],
            "results": per_result,
            "credits_used_this_call": credits,
            "usage": _usage_snapshot(self._ctx, cache=cache),
            "cache": cache,
            "note": "" if per_result else EMPTY_SEARCH_NOTE,
        }

    def _shape_scrape(self, echoed: str, mapped: dict[str, Any], *, cache: str) -> dict[str, Any]:
        cap = self._ctx.settings.firecrawl_max_markdown_chars
        full = mapped.get("markdown") or ""
        full_chars = _coerce_int(mapped.get("markdown_chars"))
        if full_chars is None:
            full_chars = len(full)
        truncated = full_chars > cap
        out_md = _truncate_markdown(full, cap) if truncated else full
        out_md_chars = len(out_md) if truncated else full_chars
        credits = int(mapped.get("credits_used_this_call") or 0)
        return {
            "url": echoed,
            "final_url": mapped.get("final_url") or echoed,
            "title": mapped.get("title") or "",
            "markdown": out_md,
            "markdown_chars": full_chars,
            "markdown_truncated_chars": out_md_chars,
            "truncated": truncated,
            "metadata": mapped.get("metadata") or {},
            "credits_used_this_call": credits,
            "usage": _usage_snapshot(self._ctx, cache=cache),
            "cache": cache,
            "note": "" if out_md.strip() else EMPTY_SCRAPE_NOTE,
        }


# -- module helpers -----------------------------------------------------------


def _reapply_truncation(result: dict[str, Any], cap: int) -> dict[str, Any]:
    """Recompute markdown truncation at presentation time from the cached full body."""
    out = dict(result)
    full = out.get("markdown") or ""
    full_chars = out.get("markdown_chars")
    if not isinstance(full_chars, int):
        full_chars = len(full)
    truncated = full_chars > cap
    if truncated:
        out["markdown"] = _truncate_markdown(full, cap)
        out["markdown_truncated_chars"] = len(out["markdown"])
    else:
        out["markdown_truncated_chars"] = full_chars
    out["truncated"] = truncated
    return out


def _usage_snapshot(ctx: "ProviderContext", *, cache: str) -> dict[str, Any]:  # type: ignore[name-defined]
    """Compose the SPEC §3.3/§3.4 `usage` block from the credit tracker."""
    source = "cached" if cache == "hit" else "live"
    snap = ctx.budget.firecrawl_tracker.snapshot(source=source)
    return {
        "provider": "firecrawl",
        "remaining_credits": snap.get("remaining_credits"),
        "plan_credits": snap.get("plan_credits"),
        "billing_period_end": snap.get("billing_period_end"),
        "source": snap.get("source", source),
        "probe_note": snap.get("probe_note", ""),
    }


def _is_http_url(url: str) -> bool:
    """Accept http(s) only; reject file://, ftp://, javascript:, data:, etc."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
