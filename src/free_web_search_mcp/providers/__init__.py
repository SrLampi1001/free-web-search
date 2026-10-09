"""Provider registry and `build_all(settings, deps)` factory.

Per SPEC §2 / §6: the lifespan calls `build_all(...)` exactly once at startup
with the shared cache, rate-limiters map, budget, and httpx client, plus the
provider-specific settings. The factory instantiates only the providers whose
credentials are configured and returns a `dict[ProviderName, Provider]`. Tools
read this dict from the lifespan context.

Phase A wires up the registry shape and the unconfigured / DDG paths.
Phase B sub-agents fill the Tavily / Firecrawl adapters.
Phase C sub-agents wire router + chain + facade around the populated dict.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .base import ProviderName

if TYPE_CHECKING:
    import httpx

    from ..budget import Budget
    from ..cache import DiskCache
    from ..rate_limit import MinIntervalLimiter
    from ..settings import Settings

log = logging.getLogger(__name__)


@dataclass
class ProviderContext:
    """Bundle of lifespan-owned state handed to each provider adapter."""

    settings: "Settings"
    cache: "DiskCache"
    rate_limiters: dict[ProviderName, "MinIntervalLimiter"]
    budget: "Budget"
    http_client: "httpx.AsyncClient"


def build_all(ctx: ProviderContext) -> dict[ProviderName, Any]:
    """Construct only the providers whose credentials are configured.

    Phase A returns the DDG provider unconditionally (no credentials needed).
    Phase B adds `tavily` and `firecrawl` adapters behind their credentials.
    """
    providers: dict[ProviderName, Any] = {}
    if ctx.settings.ddg_enabled:
        from .duckduckgo import DuckDuckGoProvider  # Phase A ships this

        providers[ProviderName.DUCKDUCKGO] = DuckDuckGoProvider(ctx)
    if ctx.settings.tavily_enabled:
        from .tavily import TavilyProvider  # Phase B (Tavily)

        providers[ProviderName.TAVILY] = TavilyProvider(ctx)
    if ctx.settings.firecrawl_enabled:
        from .firecrawl import FirecrawlProvider  # Phase B (Firecrawl)

        providers[ProviderName.FIRECRAWL] = FirecrawlProvider(ctx)

    log.info(
        "providers registered: %s",
        sorted(p.value for p in providers.keys()),
    )
    return providers


__all__ = ["ProviderContext", "build_all", "ProviderName"]
