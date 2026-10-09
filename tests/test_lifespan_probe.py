"""Tests for the SPEC §8 Firecrawl startup probe in `server._hydrate_firecrawl_budget`."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from free_web_search_mcp.budget import Budget
from free_web_search_mcp.providers.base import (
    ProviderAuthError,
    ProviderName,
    ProviderQuotaError,
    ProviderUnavailableError,
)
from free_web_search_mcp.server import _hydrate_firecrawl_budget


class FakeFirecrawl:
    """Minimal shape that matches the slice of the real adapter `_hydrate` reads."""

    def __init__(
        self,
        *,
        return_snapshot: dict[str, Any] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._snapshot = return_snapshot
        self._exc = raise_exc
        self.calls = 0

    async def credit_usage(self) -> dict[str, Any]:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._snapshot or {
            "remaining_credits": 950,
            "plan_credits": 1000,
            "billing_period_end": "2026-11-01T00:00:00+00:00",
        }


@pytest.mark.asyncio
async def test_no_op_when_firecrawl_not_registered() -> None:
    budget = Budget(firecrawl_configured=False)
    await _hydrate_firecrawl_budget({}, budget)
    assert budget.firecrawl_tracker.remaining_credits is None
    assert budget.firecrawl_tracker.sync_ok is False


@pytest.mark.asyncio
async def test_probe_populates_remaining_credits_on_success() -> None:
    fc = FakeFirecrawl()
    budget = Budget(firecrawl_configured=True)
    await _hydrate_firecrawl_budget({ProviderName.FIRECRAWL: fc}, budget)
    assert fc.calls == 1
    assert budget.firecrawl_tracker.remaining_credits == 950
    assert budget.firecrawl_tracker.plan_credits == 1000
    assert budget.firecrawl_tracker.billing_period_end == "2026-11-01T00:00:00+00:00"
    assert budget.firecrawl_tracker.sync_ok is True


@pytest.mark.asyncio
async def test_probe_marks_failed_on_auth() -> None:
    fc = FakeFirecrawl(raise_exc=ProviderAuthError("invalid key"))
    budget = Budget(firecrawl_configured=True)
    await _hydrate_firecrawl_budget({ProviderName.FIRECRAWL: fc}, budget)
    assert "auth rejected at startup" in budget.firecrawl_tracker.initial_probe_note
    assert budget.firecrawl_tracker.remaining_credits is None


@pytest.mark.asyncio
async def test_probe_marks_failed_on_quota() -> None:
    fc = FakeFirecrawl(raise_exc=ProviderQuotaError("402 out of credits"))
    budget = Budget(firecrawl_configured=True)
    await _hydrate_firecrawl_budget({ProviderName.FIRECRAWL: fc}, budget)
    assert "out of credits at startup" in budget.firecrawl_tracker.initial_probe_note


@pytest.mark.asyncio
async def test_probe_marks_failed_on_transport() -> None:
    fc = FakeFirecrawl(raise_exc=ProviderUnavailableError("connection refused"))
    budget = Budget(firecrawl_configured=True)
    await _hydrate_firecrawl_budget({ProviderName.FIRECRAWL: fc}, budget)
    assert "transport error" in budget.firecrawl_tracker.initial_probe_note


@pytest.mark.asyncio
async def test_probe_swallows_unexpected_exception() -> None:
    """Defensive: probe must never break server startup; unknown exc → graceful note."""

    class WeirdError(RuntimeError):
        pass

    fc = FakeFirecrawl(raise_exc=WeirdError("something exotic"))
    budget = Budget(firecrawl_configured=True)
    await _hydrate_firecrawl_budget({ProviderName.FIRECRAWL: fc}, budget)
    assert "something exotic" in budget.firecrawl_tracker.initial_probe_note
    assert budget.firecrawl_tracker.remaining_credits is None
