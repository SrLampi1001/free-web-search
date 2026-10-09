"""Prompt bodies from SPEC §5 — plan guides that steer the model through
the orchestrator's route/chain/budget rules."""
from __future__ import annotations


def register_prompts(mcp) -> None:
    """Bind the three SPEC §5 prompts to `mcp`."""

    @mcp.prompt(
        name="route_query",
        description=(
            "Plan a tool call for a topic: budget check, tool pick by intent "
            "shape, and rephrase-once fallback rules."
        ),
    )
    def route_query(topic: str) -> str:
        return (
            f"You are about to research the topic '{topic}'.\n"
            "1. Call providers://budget. Read `providers.tavily.calls_this_session` and `providers.firecrawl.remaining_credits`. If Firecrawl is below 50 credits, prefer the free `entity_search` and `pro_search` paths.\n"
            "2. Pick the tool: canonical entity name (person, place, product, standard) -> `entity_search`; question-shaped / long-tail / general -> `pro_search`; needs full page body or complex queries -> `deep_search`.\n"
            "3. Call the chosen tool with a canonical topic name (NOT a question). Pass the recommended defaults (count=5 / max_results=5).\n"
            "4. If the result list is empty (`results: []` and `note` non-empty), rephrase once as a canonical topic name and call the same tool again.\n"
            "5. If the tool returned `provider_used` different from what you called (i.e. the orchestrator fell through), the empty-result rule still applies - rephrase once on the new provider.\n"
            "6. If the chosen tool raised a `ToolError`, STOP and audit_budget before retrying. Do not silently chain retry - the orchestrator already tried the chain.\n"
            "7. Quote sparingly. Cite only URLs the tool actually returned.\n"
        )

    @mcp.prompt(
        name="research_brief",
        description=(
            "Multi-step research plan: audit budget, route, execute, and "
            "re-route on empty or failure."
        ),
    )
    def research_brief(topic: str) -> str:
        return (
            f"Research the topic '{topic}' thoroughly.\n"
            "1. Run `audit_budget` to learn the budget posture for the session.\n"
            "2. Run `route_query('{topic}')` to get the tool selection plan.\n"
            "3. Execute the chosen tool. If the result is empty, follow `route_query` step 4 (rephrase once).\n"
            "4. If `route_query`'s chosen tool raised a `ToolError` (e.g. rate-limited), re-call `route_query` with the narrowed scope (e.g. one specific URL candidate) so the planner can pick `scrape_url` or `extract_urls` instead.\n"
            "5. If you still don't have an answer after two tool calls, answer from your own knowledge and clearly state that the search returned no results.\n"
            "6. Cite sparingly. Every URL you cite must appear in a tool response.\n"
        )

    @mcp.prompt(
        name="audit_budget",
        description=(
            "Read-only plan that calls credit_status and "
            "providers://budget, then narrates the recommended mode for "
            "the rest of the session."
        ),
    )
    def audit_budget() -> str:
        return (
            "Audit the search budget for this session.\n"
            "1. Call `credit_status`. Capture `providers.tavily.calls_this_session`, `providers.firecrawl.remaining_credits`, `providers.firecrawl.billing_period_end`, and the `recommendation` block.\n"
            "2. Surface a 3-line summary: (a) free tool posture, (b) Tavily calls so far, (c) Firecrawl remaining vs. plan.\n"
            "3. Recommend a mode for the rest of the session: 'free-only' (Firecrawl below 50 AND Tavily above 200 calls), 'paid-mixed' (default), 'deep-only' (Firecrawl healthy AND the topic is complex enough to need it).\n"
            "4. If the recommendation says 'free-only', prefer `entity_search` for any entity-shaped lookup until the next audit.\n"
        )
