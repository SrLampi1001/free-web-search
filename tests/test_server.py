"""Phase A smoke test: the FastMCP app imports, advertises the expected stub
shape, and `/health` returns 200. Real tool semantics land in Phase D.
"""
from __future__ import annotations

import pytest
from fastmcp import Client

from free_web_search_mcp.server import INSTRUCTIONS, mcp


@pytest.mark.asyncio
async def test_lists_zero_tools_until_phase_d() -> None:
    """Phase A registers stub tool bodies; Phase D replaces them with real ones.

    The stub tools raise `NotImplementedError` on invocation, so we assert on
    `list_tools()` shape only — not on invocations.
    """
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(
        ["entity_search", "pro_search", "deep_search",
         "scrape_url", "extract_urls", "credit_status"]
    )


@pytest.mark.asyncio
async def test_lists_three_prompts_until_phase_e() -> None:
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
    names = sorted(p.name for p in prompts)
    assert names == sorted(["route_query", "research_brief", "audit_budget"])


@pytest.mark.asyncio
async def test_lists_four_resources_until_phase_e() -> None:
    async with Client(mcp) as client:
        resources = await client.list_resources()
    uris = sorted(str(r.uri) for r in resources)
    assert uris == sorted([
        "providers://status",
        "providers://budget",
        "providers://schema",
        "cache://stats",
    ])


def test_instructions_present() -> None:
    assert "Six tools" in INSTRUCTIONS
    assert "Three prompts" in INSTRUCTIONS
    assert "Four resources" in INSTRUCTIONS
