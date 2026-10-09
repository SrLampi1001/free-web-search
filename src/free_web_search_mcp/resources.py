"""Resource stubs — Phase E sub-agent `E-resources` fills these in."""
from __future__ import annotations


def register_resources(mcp) -> None:
    """Phase E replaces this implementation with the four SPEC §4 resources."""

    @mcp.resource(
        uri="providers://status",
        name="providers_status",
        mime_type="application/json",
        description="Phase E will replace this stub with the SPEC §4.1 status shape.",
    )
    def providers_status() -> dict:
        return {"phase": "A", "note": "stub; replace via Phase E sub-agent."}

    @mcp.resource(
        uri="providers://budget",
        name="providers_budget",
        mime_type="application/json",
        description="Phase E will replace this stub with the SPEC §4.2 budget shape.",
    )
    def providers_budget() -> dict:
        return {"phase": "A", "note": "stub; replace via Phase E sub-agent."}

    @mcp.resource(
        uri="providers://schema",
        name="providers_schema",
        mime_type="application/json",
        description="Phase E will replace this stub with the SPEC §4.3 schema shape.",
    )
    def providers_schema() -> dict:
        return {"phase": "A", "note": "stub; replace via Phase E sub-agent."}

    @mcp.resource(
        uri="cache://stats",
        name="cache_stats",
        mime_type="application/json",
        description="Phase E will replace this stub with the SPEC §4.4 cache stats shape.",
    )
    def cache_stats() -> dict:
        return {"phase": "A", "note": "stub; replace via Phase E sub-agent."}
