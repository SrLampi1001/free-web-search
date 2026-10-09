"""Async minimum-interval rate limiter, one per provider.

Per SPEC §5 / §8: the orchestrator holds one `MinIntervalLimiter` per provider,
keyed by `ProviderName`. Each provider's call site awaits `.wait()` before the
upstream HTTP request so a runaway agent loop can't burst the budget out of a
single process. Concurrent calls queue on the per-provider mutex.
"""
from __future__ import annotations

import asyncio
import time


class MinIntervalLimiter:
    def __init__(self, min_interval_ms: int):
        self._min = max(0.0, min_interval_ms / 1000.0)
        self._lock = asyncio.Lock()
        self._last = 0.0

    @property
    def min_interval_s(self) -> float:
        return self._min

    async def wait(self) -> None:
        if self._min <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._min - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
