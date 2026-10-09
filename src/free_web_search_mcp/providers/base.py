"""Provider ABC + exception taxonomy + ProviderName enum.

Per SPEC §2 / §6: every search upstreams in the orchestrator implements the
`Provider` interface. The interface is intentionally narrow: `search`,
`fetch_url`, `fetch_urls`, and `status`. Adapters translate from upstream
errors (`TavilyAuthError`, `FirecrawlCreditsError`, …) to the orchestrator's
neutral exception taxonomy so the chain can decide whether to fall through.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ProviderName(str, Enum):
    DUCKDUCKGO = "duckduckgo"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"


# --- Orchestrator-neutral exception taxonomy --------------------------------


class ProviderError(RuntimeError):
    """Base class for all provider errors. Adapters convert upstream-specific
    errors into this taxonomy at the boundary."""


class ProviderAuthError(ProviderError):
    """The provider rejected the credentials (HTTP 401, etc.)."""


class ProviderQuotaError(ProviderError):
    """The provider reports the budget is exhausted (HTTP 402, 432, 433, …)."""


class ProviderRateLimitError(ProviderError):
    """The provider rate-limited this key (HTTP 429)."""


class ProviderUnavailableError(ProviderError):
    """The provider is currently unreachable (transient transport / network error).

    The chain does NOT engage on transport errors — the orchestrator surfaces
    these immediately so the model's budget signal stays honest.
    """


class ProviderEmptyResults(ProviderError):
    """The provider returned zero results. Not really an error; used by the
    chain to mean "don't fall through, this is by design" when an adapter
    wants to communicate empty upstream without raising the chain."""


class ProviderABC(Protocol):
    """Adapter contract. Every provider implements `search`; only some implement
    `fetch_url` and `fetch_urls`. The orchestrator's chain and facade call these
    through the ABC and check whether a given adapter supports the operation.
    """

    name: ProviderName
    configured: bool

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        force_refresh: bool,
        depth: str | None = None,
        include_markdown: bool = True,
    ) -> dict[str, Any]:
        """Run a web search; return the SPEC §3 mapped response shape."""

    async def fetch_url(self, *, url: str, force_refresh: bool) -> dict[str, Any]:
        """Fetch a single URL's full body; return the SPEC §3.4 mapped shape."""

    async def fetch_urls(self, *, urls: list[str], force_refresh: bool) -> dict[str, Any]:
        """Batch-extract a list of URLs; return the SPEC §3.5 mapped shape."""

    def supports(self, operation: str) -> bool:
        """Return True if the adapter implements `operation` (`search` /
        `fetch_url` / `fetch_urls`)."""
