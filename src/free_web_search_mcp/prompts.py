"""Prompt stubs — Phase E sub-agent `E-prompts` fills these with the bodies in SPEC §5."""
from __future__ import annotations


def register_prompts(mcp) -> None:
    """Phase E replaces this implementation with the three SPEC §5 prompts."""

    @mcp.prompt(
        name="route_query",
        description="Phase E will replace this stub with the SPEC §5.1 prompt body.",
    )
    def route_query(topic: str) -> str:
        return f"(stub) routing plan for {topic!r}; replace via Phase E sub-agent."

    @mcp.prompt(
        name="research_brief",
        description="Phase E will replace this stub with the SPEC §5.2 prompt body.",
    )
    def research_brief(topic: str) -> str:
        return f"(stub) research brief for {topic!r}; replace via Phase E sub-agent."

    @mcp.prompt(
        name="audit_budget",
        description="Phase E will replace this stub with the SPEC §5.3 prompt body.",
    )
    def audit_budget() -> str:
        return "(stub) budget audit plan; replace via Phase E sub-agent."
