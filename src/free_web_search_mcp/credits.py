"""In-process credit tracker for the Firecrawl budget.

Per SPEC §8 / `fireclaw-search-mcp::credits`: Firecrawl exposes
`/v2/team/credit-usage`; the tracker is hydrated once at startup, then
decremented by each call's `creditsUsed`. The orchestrator's `Budget`
(read-write side) owns this tracker; the read-only `credit_status()` surface
reads its snapshot.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class ToolBudgetError(RuntimeError):
    """Raised when the credit guard refuses a call; surfaces as a `ToolError`."""


@dataclass
class CreditTracker:
    """Thread-safe wrapper around the local credit snapshot."""

    remaining_credits: int | None = None
    plan_credits: int | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    last_sync_unix: float = 0.0
    sync_ok: bool = False
    initial_probe_note: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hydrate(self, snapshot: dict[str, Any]) -> None:
        """Apply the result of a `/v2/team/credit-usage` call."""
        with self._lock:
            self.remaining_credits = snapshot.get("remaining_credits")
            self.plan_credits = snapshot.get("plan_credits")
            self.billing_period_start = snapshot.get("billing_period_start")
            self.billing_period_end = snapshot.get("billing_period_end")
            self.last_sync_unix = _now()
            self.sync_ok = self.remaining_credits is not None
            if self.sync_ok:
                self.initial_probe_note = ""

    def mark_probe_failed(self, error: str) -> None:
        with self._lock:
            self.sync_ok = False
            if not self.initial_probe_note:
                self.initial_probe_note = (
                    f"credits unknown — startup probe failed: {error}. "
                    f"The first tool call will proceed but cannot be guarded."
                )

    def charge(self, used: int) -> None:
        if used <= 0:
            return
        with self._lock:
            if self.remaining_credits is not None:
                self.remaining_credits = max(0, self.remaining_credits - used)

    def guard(self, min_credits: int) -> None:
        """Raise a model-friendly error if the local counter is too low.

        Skips the check entirely if the counter is `None` (probe never
        succeeded); the real API error from the upstream call will surface and
        the model can react then.
        """
        if min_credits <= 0:
            return
        with self._lock:
            r = self.remaining_credits
            resets = self.billing_period_end
        if r is None:
            return
        if r <= 0:
            raise ToolBudgetError(
                f"Firecrawl is out of credits for this billing period "
                f"(resets {resets or 'next cycle'}). Switch to a free sibling "
                f"search tool and wait for the reset."
            )
        if r < min_credits:
            raise ToolBudgetError(
                f"Firecrawl has only {r} credits left (the configured "
                f"minimum is {min_credits}). Switch to a free sibling search "
                f"tool or wait for the billing period to reset on "
                f"{resets or 'the next cycle'}."
            )

    def snapshot(self, *, source: str) -> dict[str, Any]:
        with self._lock:
            return {
                "remaining_credits": self.remaining_credits,
                "plan_credits": self.plan_credits,
                "billing_period_start": self.billing_period_start,
                "billing_period_end": self.billing_period_end,
                "source": source,
                "probe_note": self.initial_probe_note,
            }


def _now() -> float:
    import time
    return time.time()
