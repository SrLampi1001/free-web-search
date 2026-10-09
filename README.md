# Free Web Search MCP

A [Model Context Protocol](https://modelcontextprotocol.io/) server that aggregates three web-search backends — DuckDuckGo Instant Answer (free, no key), Tavily (`api.tavily.com`, bearer), and Firecrawl (`api.firecrawl.dev/v2`, bearer) — into a unified free-first surface with explicit fall-through on Auth/Quota/RateLimit errors. Built on **FastMCP 4.0.11** against the **MCP 2026-07-28** revision. Runs over Streamable HTTP (default) or stdio.

## What you get

- **6 tools:** `entity_search`, `pro_search`, `deep_search`, `scrape_url`, `extract_urls`, `credit_status`.
- **4 resources:** `providers://status`, `providers://budget`, `providers://schema`, `cache://stats`.
- **3 prompts:** `route_query`, `research_brief`, `audit_budget`.
- **No auth on the server.** Loopback bind by design. Expose via **VS Code port forwarding** (recommended) or ngrok.

## Quick start

```bash
# Install (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure at least one paid provider key (DDG works without one)
cp .env.example .env
$EDITOR .env

# Run (Streamable HTTP on http://127.0.0.1:8000/mcp)
python3 -m free_web_search_mcp
```

Then either:

- **Local use** — point an MCP client at `http://127.0.0.1:8000/mcp` on the same machine.
- **VS Code port forwarding** — open the Ports panel, forward `8000`, copy the tunnel URL, point your client at `<forwarded-url>/mcp`. See `docs/DEPLOYMENT.md`.

## Documentation

- [`docs/SPECS.md`](./docs/SPECS.md) — the contract this server commits to: protocol, tool/resource/prompt schemas, response mapping, failure modes, acceptance criteria.
- [`docs/PLAN.md`](./docs/PLAN.md) — the implementation plan: phases, sub-agent ownership, verification gates.
- [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md) — connection recipes for every supported client.
- [`.agents/skills/`](./.agents/skills/) — the reference skill (`mcp-fastmcp-2026/`) and two project skills (`web-search-routing/`, `web-search-budgeting/`) shipped with the repo.

## Testing

```bash
.venv/bin/pytest                          # unit tests, hermetic
FWS_NETWORK_TESTS=1 .venv/bin/pytest      # + integration tests, real upstreams
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .   # static
```

After starting the server:

```bash
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp   # wire probe
```

## Why an aggregator and not three separate MCPs

The three siblings (`duck-search-mcp`, `tavily-search-mcp`, `fireclaw-search-mcp`) were designed as a tripod: each ships a single shape and every prompt in each sibling tells the model to "switch to a free sibling search tool" when its own budget is tight. This server is the seat that tripod sits on. The siblings stay standalone-deployable; this orchestrator gives an agent a single connection with the union surface and explicit routing rules.

Routing decisions are encoded once in `src/free_web_search_mcp/providers/router.py` and exercised through `facade.py::orchestrate`. Failure of one paid provider (Auth, Quota, RateLimit) falls through to the **other paid provider of the same shape**, never silently back to DDG, because DDG has a different surface and a silent shape change would be worse than a clear error.

## Notes

- **No live credit endpoint for Tavily.** Tavily has no `remaining_credits` endpoint; the orchestrator counts calls in-process. `credit_status().providers.tavily.calls_this_session` is the local counter.
- **Live credit probe for Firecrawl.** Firecrawl exposes `/v2/team/credit-usage`; the orchestrator hydrates the local `CreditTracker` once at startup and decrements per call.
- **Privacy.** Per [Tavily's Terms of Service](https://www.tavily.com/terms) §6.5 / §9.2, Tavily and its third-party AI providers may retain and use any Customer Input and Outputs to train models. Treat Tavily queries as potentially visible to Tavily. Per [Firecrawl's Terms](https://firecrawl.dev/terms), the same applies for Firecrawl. DuckDuckGo Instant Answer is unauthenticated and the orchestrator identifies itself via the `t=free-web-search-mcp` parameter. The on-disk cache is operator-local (gitignored, under `FWS_CACHE_DIR`); neither paid provider sees what the cache stores.
- **Auth.** The Authorization header is the wire contract for Tavily and Firecrawl (`Authorization: Bearer …`). Those tokens never appear in the request body. The orchestrator itself binds to `127.0.0.1`; tunnel auth (VS Code or ngrok) is the only network auth.

## License

MIT.
