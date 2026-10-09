"""Tavily v1 provider adapter (SPEC §3.2 + §3.5).

Adapter over the sibling `tavily-search-mcp::tavily.py` HTTP client +
mappers. Adds caching, pacing, counter charging, URL validation, the
SPEC envelope, and maps Tavily/httpx errors to the orchestrator's
neutral taxonomy at the boundary.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx

from ..normalize import canonical_batch_key, normalize_query
from .base import (
    ProviderAuthError,
    ProviderError,
    ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.tavily.com"
USER_AGENT = "free-web-search-mcp/0.1"

EMPTY_SEARCH_NOTE = "Tavily returned no results for this query. Rephrase once as a specific topic name with concrete terms, and if that also comes back empty, answer from your own knowledge and say so."
EMPTY_EXTRACT_NOTE = "Tavily returned no extracted bodies for these URLs. They may be paywalled, behind JS shells, or unsupported; the failed_results array carries the per-URL error."


# --- exceptions ---


class TavilyError(RuntimeError):
    """Tavily transport or non-2xx HTTP error."""


class TavilyAuthError(TavilyError):
    """HTTP 401."""


class TavilyRateLimitError(TavilyError):
    """HTTP 429."""


class TavilyQuotaError(TavilyError):
    """HTTP 432/433 (Key/Plan or PayGo limit)."""


class TavilyUnavailableError(TavilyError):
    """Transport-layer failure (timeout, connect, HTTP)."""


# --- I/O ---

_TRUNCATION_MARKER = "\n\n[…truncated to fit the context budget; call extract_url on the same URL with a narrower focus, or ask for a specific section…]"


def _truncate_raw(text: str, max_chars: int) -> str:
    room = max_chars - len(_TRUNCATION_MARKER)
    if room <= 0:
        return text[:max_chars]
    return text[:room] + _TRUNCATION_MARKER


class TavilyClient:
    """Async HTTP client for `POST /search` and `POST /extract`."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        max_raw_chars: int = 8000,
    ) -> None:
        if not api_key:
            raise TavilyError("TAVILY_API_KEY is not set; tools will refuse to call Tavily")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_raw_chars = max_raw_chars
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
            raise TavilyError("Tavily client not attached to a shared httpx session")
        return self._client

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
        include_answer: bool | str = "basic",
        include_raw_content: bool | str = "markdown",
        include_favicon: bool = True,
        chunks_per_source: int = 3,
    ) -> dict[str, Any]:
        """Run Tavily `POST /search`. Returns the mapped upstream shape."""
        body: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(int(max_results), 20)),
            "search_depth": search_depth,
            "topic": topic,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
            "include_favicon": include_favicon,
            "chunks_per_source": max(1, min(int(chunks_per_source), 3)),
        }
        payload = await self._post("/search", body)
        return _map_search_response(payload, self._max_raw_chars)

    async def extract(
        self,
        urls: list[str],
        *,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_favicon: bool = True,
    ) -> dict[str, Any]:
        """Run Tavily `POST /extract` for 1-20 URLs in one request."""
        body: dict[str, Any] = {
            "urls": list(urls),
            "extract_depth": extract_depth,
            "format": format,
            "include_favicon": include_favicon,
        }
        payload = await self._post("/extract", body)
        return _map_extract_response(payload, self._max_raw_chars)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self._base_url + path,
                json=body,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.TimeoutException as e:
            raise TavilyUnavailableError(f"Tavily transport timeout: {e}") from e
        except httpx.ConnectError as e:
            raise TavilyUnavailableError(f"Tavily connect error: {e}") from e
        except httpx.HTTPError as e:
            raise TavilyUnavailableError(f"Tavily transport error: {e}") from e
        return _parse_response(resp)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """Validate an HTTP response and return the JSON body. Raises typed errors."""
    if resp.status_code == 401:
        raise TavilyAuthError("Tavily rejected the API key (401 Unauthorized)")
    if resp.status_code in (432, 433):
        raise TavilyQuotaError(
            f"Tavily quota exhausted (HTTP {resp.status_code}); check the dashboard."
        )
    if resp.status_code == 429:
        raise TavilyRateLimitError("Tavily rate-limited this key (429 Too Many Requests)")
    if resp.status_code >= 400:
        text = (resp.text or "")[:200]
        raise TavilyError(f"Tavily upstream HTTP {resp.status_code}: {text!r}")
    try:
        body = resp.json()
    except ValueError as e:
        raise TavilyError(f"Tavily returned non-JSON body (status {resp.status_code}): {e}") from e
    return body


# --- mappings ---

def _map_search_response(payload: dict[str, Any], max_raw_chars: int) -> dict[str, Any]:
    """Project `/search` payload onto the cacheable upstream shape."""
    results_raw = payload.get("results") or []
    results: list[dict[str, Any]] = []
    for entry in results_raw:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        raw = entry.get("raw_content")
        raw_text = raw if isinstance(raw, str) else ""
        raw_chars = len(raw_text)
        truncated = raw_chars > max_raw_chars
        if truncated:
            raw_text = _truncate_raw(raw_text, max_raw_chars)
        results.append(
            {
                "title": entry.get("title") or "",
                "url": url,
                "content": entry.get("content") or "",
                "score": entry.get("score"),
                "raw_content": raw_text if raw else "",
                "raw_content_chars": raw_chars,
                "raw_content_truncated_chars": len(raw_text) if truncated else raw_chars,
                "truncated": truncated,
                "published_date": entry.get("published_date"),
                "favicon": entry.get("favicon"),
                "id": entry.get("id"),
            }
        )

    return {
        "query": payload.get("query") or "",
        "answer": payload.get("answer") if isinstance(payload.get("answer"), str) else "",
        "results": results,
        "request_id": payload.get("request_id") or "",
        "response_time": payload.get("response_time"),
        "raw_result_count": len(results_raw),
    }


def _map_extract_response(payload: dict[str, Any], max_raw_chars: int) -> dict[str, Any]:
    """Project `/extract` payload onto the cacheable upstream shape."""
    results_in = payload.get("results") or []
    results: list[dict[str, Any]] = []
    for entry in results_in:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        raw = entry.get("raw_content")
        raw_text = raw if isinstance(raw, str) else ""
        raw_chars = len(raw_text)
        truncated = raw_chars > max_raw_chars
        if truncated:
            raw_text = _truncate_raw(raw_text, max_raw_chars)
        results.append(
            {
                "url": url,
                "raw_content": raw_text,
                "raw_content_chars": raw_chars,
                "raw_content_truncated_chars": len(raw_text) if truncated else raw_chars,
                "truncated": truncated,
                "images": entry.get("images") or [],
                "favicon": entry.get("favicon"),
            }
        )
    failed_in = payload.get("failed_results") or []
    failed: list[dict[str, Any]] = []
    for entry in failed_in:
        if not isinstance(entry, dict):
            continue
        failed.append({"url": entry.get("url") or "", "error": entry.get("error") or ""})

    return {
        "results": results,
        "failed_results": failed,
        "request_id": payload.get("request_id") or "",
        "response_time": payload.get("response_time"),
        "raw_result_count": len(results_in),
        "raw_failed_count": len(failed_in),
    }


# --- adapter ---

class TavilyProvider:
    """SPEC §3.2 (`pro_search`) and §3.5 (`extract_urls`) adapter."""

    name = ProviderName.TAVILY

    def __init__(self, ctx: "ProviderContext") -> None:  # type: ignore[name-defined]
        self._ctx = ctx
        settings = ctx.settings
        self.configured = bool(settings.tavily_api_key)
        if self.configured:
            self._client: TavilyClient | None = TavilyClient(
                api_key=settings.tavily_api_key,
                base_url=settings.tavily_base_url,
                timeout_s=settings.http_timeout_s,
                max_raw_chars=settings.tavily_max_raw_chars,
            )
            self._client.attach(ctx.http_client)
        else:
            self._client = None

    def supports(self, operation: str) -> bool:
        return operation in ("search", "fetch_urls")

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        force_refresh: bool,
        depth: str | None = None,
        include_markdown: bool = True,
    ) -> dict[str, Any]:
        echoed = (query or "").strip()
        n = max(1, min(10, int(max_results) or 5))
        depth_n = depth if depth in ("basic", "advanced") else "basic"
        cache_key = f"{normalize_query(echoed)}|{depth_n}"

        async def call():
            return await self._client.search(
                echoed,
                max_results=n,
                search_depth=depth_n,
                include_answer="basic",
                include_raw_content="markdown" if include_markdown else False,
            )

        upstream, state = await self._run_with_cache(call, cache_key, "search", force_refresh)
        return self._search_envelope(upstream, echoed, n, state)

    async def fetch_urls(
        self,
        *,
        urls: list[str],
        force_refresh: bool,
    ) -> dict[str, Any]:
        cleaned = self._clean_urls(urls)
        cache_key = canonical_batch_key(cleaned)

        async def call():
            return await self._client.extract(
                urls=cleaned,
                extract_depth="basic",
                format="markdown",
                include_favicon=True,
            )

        upstream, state = await self._run_with_cache(call, cache_key, "extract", force_refresh)
        return self._extract_envelope(upstream, cleaned, state)

    # -- helpers --

    def _check_guard(self) -> None:
        settings = self._ctx.settings
        max_calls = settings.tavily_max_calls
        if max_calls <= 0:
            return
        if self._ctx.budget.tavily_counter.calls >= max_calls:
            raise ProviderQuotaError(
                "Tavily in-process call counter has reached the configured "
                "FWS_TAVILY_MAX_CALLS limit; switch to a free sibling tool or raise the limit."
            )

    async def _run_with_cache(
        self, upstream_call, key: str, kind: str, force_refresh: bool
    ) -> tuple[dict[str, Any], str]:
        """Cache lookup + guard + rate-limit + upstream + charge. Returns (upstream, cache_state)."""
        if not force_refresh:
            cached = self._ctx.cache.get("tavily", key)
            if cached is not None and cached.kind == kind:
                return cached.response, "hit"
        self._check_guard()
        await self._ctx.rate_limiters[ProviderName.TAVILY].wait()
        upstream = await self._call_upstream(upstream_call())
        self._ctx.cache.set("tavily", key, kind, upstream)
        self._ctx.budget.tavily_counter.charge(1)
        return upstream, "bypassed" if force_refresh else "miss"

    @staticmethod
    async def _call_upstream(coro):
        """Await an upstream coroutine, mapping Tavily / httpx errors to the
        orchestrator's neutral taxonomy at the adapter boundary."""
        try:
            return await coro
        except TavilyAuthError as e:
            raise ProviderAuthError(f"Tavily rejected the API key: {e}") from e
        except TavilyQuotaError as e:
            raise ProviderQuotaError(f"Tavily quota exhausted: {e}") from e
        except TavilyRateLimitError as e:
            raise ProviderRateLimitError(
                f"Tavily rate-limited this key; wait before retrying: {e}"
            ) from e
        except TavilyUnavailableError as e:
            raise ProviderUnavailableError(str(e)) from e
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
            raise ProviderUnavailableError(f"Tavily transport error: {e}") from e
        except TavilyError as e:
            raise ProviderError(str(e)) from e

    def _search_envelope(
        self, upstream: dict[str, Any], echoed: str, n: int, cache_state: str
    ) -> dict[str, Any]:
        results = list(upstream.get("results") or [])[:n]
        return {
            "query": echoed,
            "provider_used": "tavily",
            "fallthroughs": [],
            "answer": upstream.get("answer", "") or "",
            "results": results,
            "request_id": upstream.get("request_id", "") or "",
            "cache": cache_state,
            "note": "" if results else EMPTY_SEARCH_NOTE,
            "usage": self._usage(),
        }

    def _extract_envelope(
        self, upstream: dict[str, Any], urls: list[str], cache_state: str
    ) -> dict[str, Any]:
        results = upstream.get("results") or []
        failed = upstream.get("failed_results") or []
        return {
            "urls": urls,
            "provider_used": "tavily",
            "fallthroughs": [],
            "results": results,
            "failed_results": failed,
            "request_id": upstream.get("request_id", "") or "",
            "cache": cache_state,
            "note": EMPTY_EXTRACT_NOTE if not results and not failed else "",
            "usage": self._usage(),
        }

    def _usage(self) -> dict[str, Any]:
        snap = self._ctx.budget.tavily_counter.snapshot()
        return {
            "provider": "tavily",
            "calls_this_session": snap["calls_this_session"],
            "billing_period_end": None,
            "note": snap["note"],
        }

    def _clean_urls(self, urls: list[str]) -> list[str]:
        if not isinstance(urls, list) or not urls:
            raise ProviderError("extract_urls requires at least one URL")
        cleaned: list[str] = []
        for u in urls:
            if not isinstance(u, str):
                raise ProviderError(f"extract_urls URL must be a string; got {type(u).__name__}")
            u = u.strip()
            if not u:
                continue
            parsed = urllib.parse.urlsplit(u)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ProviderError(f"extract_urls requires an http(s) URL; got {u!r}")
            cleaned.append(u)
        if not cleaned:
            raise ProviderError("extract_urls requires at least one non-empty URL")
        if len(cleaned) > 20:
            raise ProviderError(f"extract_urls accepts 1-20 URLs; got {len(cleaned)}")
        return cleaned


__all__ = ["TavilyProvider", "TavilyClient", "TavilyError", "TavilyAuthError", "TavilyRateLimitError", "TavilyQuotaError", "TavilyUnavailableError", "EMPTY_SEARCH_NOTE", "EMPTY_EXTRACT_NOTE"]
