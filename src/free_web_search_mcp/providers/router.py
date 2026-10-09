"""Routing stub — Phase C sub-agent implements the Strategy here."""
from __future__ import annotations

from .base import ProviderName


def pick(intent: str, providers: dict) -> "list[ProviderName]":
    """Phase C will replace this with the SPEC §6 decision matrix.

    Phase A returns a placeholder so server.py imports succeed; the smoke
    test does not exercise the router.
    """
    if not providers:
        return []
    if intent == "entity":
        return [ProviderName.DUCKDUCKGO] if ProviderName.DUCKDUCKGO in providers else []
    if intent == "general":
        return [n for n in (ProviderName.TAVILY, ProviderName.FIRECRAWL) if n in providers]
    if intent == "deep":
        return [n for n in (ProviderName.FIRECRAWL, ProviderName.TAVILY) if n in providers]
    return []
