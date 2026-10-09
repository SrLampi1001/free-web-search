"""Routing Strategy — SPEC §6 decision matrix.

Maps a caller intent to the ordered list of providers the chain should try.
Returns `[]` when no provider can serve the intent; the caller (facade) lets
`chain.try_chain` translate that into `ChainExhaustedError`.
"""
from __future__ import annotations

from typing import Literal

from .base import ProviderError, ProviderName

IntentKind = Literal["entity", "general", "deep", "scrape", "extract", "status"]


class NoProviderError(ProviderError):
    """Raised when a routing decision finds no provider that can serve the intent.

    Kept next to the routing logic because the exception is about routing,
    not about any one adapter. Tests import it from this module.
    """


def pick(
    intent: str,
    providers: dict[ProviderName, object],
) -> list[ProviderName]:
    """Return the ordered list of providers the chain should try for this intent.

    Implements SPEC §6 verbatim:

    - entity: [DUCKDUCKGO] if configured, else []
    - general: [TAVILY, FIRECRAWL] in order, skipping any not configured
    - deep: [FIRECRAWL, TAVILY] in order, skipping any not configured
    - scrape: [FIRECRAWL] if configured, else []
    - extract: [TAVILY] if configured, else []
    - status: []

    An empty `providers` dict always returns `[]`; the facade turns that into
    `ChainExhaustedError` via the chain.
    """
    if intent == "status":
        return []
    if intent == "entity":
        return [ProviderName.DUCKDUCKGO] if ProviderName.DUCKDUCKGO in providers else []
    if intent == "general":
        return [n for n in (ProviderName.TAVILY, ProviderName.FIRECRAWL) if n in providers]
    if intent == "deep":
        return [n for n in (ProviderName.FIRECRAWL, ProviderName.TAVILY) if n in providers]
    if intent == "scrape":
        return [ProviderName.FIRECRAWL] if ProviderName.FIRECRAWL in providers else []
    if intent == "extract":
        return [ProviderName.TAVILY] if ProviderName.TAVILY in providers else []
    return []


__all__ = ["IntentKind", "NoProviderError", "pick"]
