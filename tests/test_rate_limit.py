"""Tests for the per-provider minimum-interval rate limiter."""
from __future__ import annotations

import asyncio
import time

import pytest

from free_web_search_mcp.rate_limit import MinIntervalLimiter


@pytest.mark.asyncio
async def test_disabled_when_interval_zero() -> None:
    limiter = MinIntervalLimiter(min_interval_ms=0)
    for _ in range(3):
        await limiter.wait()
    # No observable delay; nothing to assert beyond "did not raise".


@pytest.mark.asyncio
async def test_serial_calls_are_spaced() -> None:
    limiter = MinIntervalLimiter(min_interval_ms=50)
    t0 = time.monotonic()
    for _ in range(3):
        await limiter.wait()
    elapsed = time.monotonic() - t0
    # 50ms between each of 3 calls => >= 100ms total spent in wait()
    assert elapsed >= 0.10


@pytest.mark.asyncio
async def test_concurrent_calls_queue() -> None:
    limiter = MinIntervalLimiter(min_interval_ms=80)
    t0 = time.monotonic()
    await asyncio.gather(*(limiter.wait() for _ in range(3)))
    elapsed = time.monotonic() - t0
    # Three concurrent waits spaced 80ms apart => >= 160ms total
    assert elapsed >= 0.15
