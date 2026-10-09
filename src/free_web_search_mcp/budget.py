"""Merged budget view across all three providers.

Per SPEC §8: the orchestrator owns one `Budget` instance that aggregates the
Tavily `CallCounter` and the Firecrawl `CreditTracker`, plus a static
'configured/available' flag for DDG (which has no key and no budget). The
`Budget` is read-only on the surface; tool implementations use the underlying
`CallCounter.charge()` / `CreditTracker.charge()` methods to update state.
`credit_status()` reads `Budget.snapshot()` to produce the merged response and
the `recommendation` block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .call_counter import CallCounter
from .credits import CreditTracker


@dataclass
class Budget:
    tavily_counter: CallCounter = field(default_factory=CallCounter)
    firecrawl_tracker: CreditTracker = field(default_factory=CreditTracker)
    tavily_configured: bool = False
    firecrawl_configured: bool = False
    ddg_configured: bool = True

    def snapshot(self) -> dict[str, Any]:
        """Read-only merged view, plus the routing recommendation block."""
        tavily = {"configured": self.tavily_configured}
        if self.tavily_configured:
            tavily.update(self.tavily_counter.snapshot())
        else:
            tavily["note"] = "TAVILY_API_KEY is not set; Tavily tools are not registered."

        firecrawl = {"configured": self.firecrawl_configured}
        if self.firecrawl_configured:
            firecrawl.update(self.firecrawl_tracker.snapshot(source="cached"))
        else:
            firecrawl["note"] = "FIRECRAWL_API_KEY is not set; Firecrawl tools are not registered."

        ddg = {
            "configured": self.ddg_configured,
            "available": self.ddg_configured,
            "note": "Free, no key; no budget signal.",
        }

        recommendation = self._build_recommendation(tavily, firecrawl)

        return {
            "providers": {
                "tavily": tavily,
                "firecrawl": firecrawl,
                "duckduckgo": ddg,
            },
            "recommendation": recommendation,
        }

    def _build_recommendation(self, tavily: dict, firecrawl: dict) -> dict[str, Any]:
        firecrawl_remaining = firecrawl.get("remaining_credits") if isinstance(firecrawl, dict) else None
        tavily_calls = tavily.get("calls_this_session", 0) if isinstance(tavily, dict) else 0

        firecrawl_low = isinstance(firecrawl_remaining, int) and firecrawl_remaining < 50
        tavily_high = isinstance(tavily_calls, int) and tavily_calls >= 200

        if firecrawl_low and tavily_high:
            entity = "entity_search"
            general = "entity_search"
            deep = "entity_search"
        elif firecrawl_low:
            entity = "entity_search"
            general = "pro_search"
            deep = "pro_search"
        else:
            entity = "entity_search"
            general = "pro_search"
            deep = "deep_search"

        return {
            "entity_queries": entity,
            "general_questions": general,
            "deep_retrieval": deep,
            "single_url": "scrape_url",
            "batch_urls": "extract_urls",
            "switch_to_free_when": {
                "firecrawl_remaining_credits_below": 50,
                "tavily_calls_this_session_above": 200,
            },
            "current_signals": {
                "firecrawl_remaining_credits": firecrawl_remaining,
                "tavily_calls_this_session": tavily_calls,
            },
            "note": "Routing rules per SPEC §6 are fixed; this block is advisory only and does not change how orchestrator tools route.",
        }
