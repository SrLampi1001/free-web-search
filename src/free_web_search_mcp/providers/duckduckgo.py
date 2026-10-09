"""DuckDuckGo Instant Answer provider adapter.

Per SPEC §3.1: the orchestrator's `entity_search` tool routes here. The DDG
backend has no Auth, Quota, or RateLimit path that warrants a fall-through, so
this adapter exposes `supports('chain') == False` and any upstream error
surfaces immediately (no chain).
"""
from __future__ import annotations

import logging

from ..ddg import fetch_ddg, map_ddg_response
from ..normalize import normalize_query
from .base import ProviderName

log = logging.getLogger(__name__)


class DuckDuckGoProvider:
    name = ProviderName.DUCKDUCKGO
    configured = True

    def __init__(self, ctx: "ProviderContext") -> None:  # type: ignore[name-defined]
        self._ctx = ctx

    def supports(self, operation: str) -> bool:
        if operation == "search":
            return True
        return False

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        force_refresh: bool,
        depth: str | None = None,
        include_markdown: bool = True,
    ) -> dict:
        """SPEC §3.1 shape: per-result `{title, url, snippet}` plus `note`."""
        ns = self.name.value
        echoed = query.strip()
        norm = normalize_query(echoed)
        n = max(1, min(10, int(max_results) or 5))
        settings = self._ctx.settings

        if not force_refresh:
            cached = self._ctx.cache.get(ns, norm)
            if cached is not None:
                resp = dict(cached.response)
                resp["query"] = echoed
                resp["source"] = "cache"
                resp["note"] = "" if resp.get("results") else resp.get("note", "")
                return resp

        await self._ctx.rate_limiters[ProviderName.DUCKDUCKGO].wait()

        try:
            payload = await fetch_ddg(echoed, client=self._ctx.http_client, timeout_s=settings.http_timeout_s)
        except Exception as e:
            log.warning("ddg: upstream error for %r: %s", echoed, e)
            raise

        results = map_ddg_response(payload)
        per_caller = results[:n]
        note = "" if results else "DuckDuckGo returned no results — rephrase once as a canonical topic name, then answer from your own knowledge."
        response = {
            "query": echoed,
            "results": per_caller,
            "note": note,
        }
        self._ctx.cache.set(ns, norm, "search", response)
        return response
