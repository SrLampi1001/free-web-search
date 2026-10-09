"""Tests for the merged `Budget` snapshot + `recommendation` block."""
from __future__ import annotations

from free_web_search_mcp.budget import Budget
from free_web_search_mcp.credits import CreditTracker
from free_web_search_mcp.call_counter import CallCounter


def test_snapshot_with_no_providers_configured() -> None:
    b = Budget()
    snap = b.snapshot()
    assert snap["providers"]["tavily"]["configured"] is False
    assert snap["providers"]["firecrawl"]["configured"] is False
    assert snap["providers"]["duckduckgo"]["configured"] is True
    assert "recommendation" in snap


def test_snapshot_with_tavily_only() -> None:
    b = Budget(tavily_configured=True)
    b.tavily_counter.charge(by=5)
    snap = b.snapshot()
    assert snap["providers"]["tavily"]["configured"] is True
    assert snap["providers"]["tavily"]["calls_this_session"] == 5


def test_snapshot_with_firecrawl_only() -> None:
    b = Budget(firecrawl_configured=True)
    b.firecrawl_tracker.hydrate({"remaining_credits": 100, "plan_credits": 1000})
    snap = b.snapshot()
    assert snap["providers"]["firecrawl"]["remaining_credits"] == 100


def test_recommendation_uses_general_when_firecrawl_low() -> None:
    b = Budget(tavily_configured=True, firecrawl_configured=True)
    b.firecrawl_tracker.hydrate({"remaining_credits": 10, "plan_credits": 1000})  # below 50
    b.tavily_counter.charge(by=5)
    r = b.snapshot()["recommendation"]
    assert r["entity_queries"] == "entity_search"
    assert r["general_questions"] == "pro_search"
    assert r["deep_retrieval"] == "pro_search"


def test_recommendation_collapses_to_free_when_both_low() -> None:
    b = Budget(tavily_configured=True, firecrawl_configured=True)
    b.firecrawl_tracker.hydrate({"remaining_credits": 10, "plan_credits": 1000})
    b.tavily_counter.charge(by=250)  # above 200
    r = b.snapshot()["recommendation"]
    assert r["entity_queries"] == "entity_search"
    assert r["general_questions"] == "entity_search"
    assert r["deep_retrieval"] == "entity_search"


def test_recommendation_default_to_paid_when_healthy() -> None:
    b = Budget(tavily_configured=True, firecrawl_configured=True)
    b.firecrawl_tracker.hydrate({"remaining_credits": 800, "plan_credits": 1000})
    b.tavily_counter.charge(by=10)
    r = b.snapshot()["recommendation"]
    assert r["entity_queries"] == "entity_search"
    assert r["general_questions"] == "pro_search"
    assert r["deep_retrieval"] == "deep_search"
