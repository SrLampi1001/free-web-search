"""In-process call counter for the Tavily budget.

Per the SPEC §8 / `tavily-search-mcp::call_counter`: Tavily has no live
remaining-credit endpoint, so the orchestrator counts calls in-process and
surfaces the snapshot in `credit_status().providers.tavily`. The counter is
intentionally not a connection-keyed singleton — sessions are gone in MCP
2026-07-28 — but its state lives in the budget state, so it is shut down with
the FastMCP server rather than leaking between restarts.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class CallCounter:
    """Thread-safe wrapper around a monotonic in-process counter."""

    _calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, by: int = 1) -> None:
        if by <= 0:
            return
        with self._lock:
            self._calls += by

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls_this_session": self._calls,
                "note": "Tavily does not expose a remaining-credit endpoint; the counter is local.",
            }

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls
