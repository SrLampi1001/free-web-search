"""Facade — `orchestrate(...)` is the single entry the tool wrappers call.

Routes via `router.pick`, runs via `chain.try_chain`, then stamps
`provider_used` / `fallthroughs` / (optional) `usage` onto the adapter's
response so the model's view is consistent regardless of which adapter
served the call.

The facade does NOT translate exceptions into MCP `ToolError`; it propagates
`ChainExhaustedError`, `ProviderError`, and `NoProviderError` upward so the
tool wrappers in `server.py` own that translation.
"""
from __future__ import annotations

import time
from typing import Any

from ..budget import Budget
from . import chain, router
from .base import ProviderName

_PROVIDER_NAME_TO_SNAPSHOT_KEY = {
    ProviderName.DUCKDUCKGO: "duckduckgo",
    ProviderName.TAVILY: "tavily",
    ProviderName.FIRECRAWL: "firecrawl",
}


async def orchestrate(
    *,
    intent: str,
    providers: dict[ProviderName, object],
    budget: Budget,
    op: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Single call site for the orchestrator's six tools.

    For `intent == "status"`: returns `budget.snapshot()` stamped with
    `called_at_unix`. The tool wrapper turns this into the SPEC §3.6 envelope.

    For other intents: picks a chain via `router.pick`, runs it via
    `chain.try_chain`, stamps `provider_used` / `fallthroughs` (overwriting
    the adapter's defaults if any), and adds a default `usage` block only if
    the adapter didn't supply one.
    """
    if intent == "status":
        snap: dict[str, Any] = dict(budget.snapshot())
        snap["called_at_unix"] = time.time()
        return snap

    ordered = router.pick(intent, providers)
    winning_name, response, fallthroughs = await chain.try_chain(
        ordered, providers, op, **kwargs  # type: ignore[arg-type]
    )

    response["provider_used"] = winning_name.value
    response["fallthroughs"] = list(fallthroughs)

    if "usage" not in response:
        snap_key = _PROVIDER_NAME_TO_SNAPSHOT_KEY.get(winning_name)
        provider_snap: dict[str, Any] = {}
        if snap_key is not None:
            provider_snap = budget.snapshot().get("providers", {}).get(snap_key, {})
        usage: dict[str, Any] = {"provider": winning_name.value}
        usage.update(provider_snap)
        response["usage"] = usage

    return response


__all__ = ["orchestrate"]
