# Plan — `free-web-search-mcp` v0.1

> Implementation plan for the spec in `docs/SPECS.md`. The SPEC is the contract; this doc is the path through it. Read §15 of the SPEC first for the criteria we are satisfying. Sub-agents in Phase B–E own non-overlapping slices; the orchestrator (this thread) integrates, runs the three verification gates, and brings up the tunnel.

## 0. Goal and non-goals

**Goal.** Ship a single FastMCP 4 / MCP 2026-07-28 server at `/home/cohorte8/Documents/ssr/projects/mcps/free-web-search-mcp/` that satisfies the seven acceptance criteria and follows the lineamiento set by the three sibling MCPs (`duck-search-mcp`, `tavily-search-mcp`, `fireclaw-search-mcp`).

**Non-goals (per SPEC §17).** No parallel upstream calls per tool call; no additional upstreams beyond DDG / Tavily / Firecrawl; no streaming; no MRTR confirms; no auth layer.

## 1. Stack (pinned)

- Python 3.10+ (tested on 3.12).
- `fastmcp==4.0.11`, `httpx>=0.27`. dev: `pytest>=8.0`, `pytest-asyncio>=0.23`. Same pins as the siblings.
- Copy `.agents/skills/mcp-fastmcp-2026/` from `duck-search-mcp` (no changes; same baseline). The audit and probe scripts at `.agents/skills/mcp-fastmcp-2026/scripts/` are the verification suite.
- Mirror the lineamiento: loopback bind, no auth, Streamable HTTP + stdio, `cache_ttl=300, cache_scope="public"` at the FastMCP constructor, lifespan-owned state.

## 2. File tree (post-build)

```
free-web-search-mcp/
├── AGENTS.md, README.md
├── .env.example, .gitignore
├── pyproject.toml
├── .agents/skills/
│   ├── mcp-fastmcp-2026/                # reference skill, copied from duck-search-mcp
│   ├── web-search-routing/SKILL.md      # authored from scratch
│   └── web-search-budgeting/SKILL.md    # authored from scratch
├── docs/{SPECS.md, PLAN.md, DEPLOYMENT.md, CHANGELOG.md}
├── src/free_web_search_mcp/
│   ├── __init__.py
│   ├── __main__.py
│   ├── server.py
│   ├── settings.py
│   ├── normalize.py
│   ├── cache.py
│   ├── rate_limit.py
│   ├── budget.py
│   ├── call_counter.py                  # Tavily counter, extracted
│   ├── credits.py                       # Firecrawl tracker, extracted
│   ├── prompts.py
│   ├── resources.py
│   ├── ddg.py
│   └── providers/
│       ├── __init__.py                  # build_all, PROVIDERS registry, ProviderName enum
│       ├── base.py                      # Provider ABC + exception taxonomy
│       ├── duckduckgo.py
│       ├── tavily.py
│       ├── firecrawl.py
│       ├── router.py
│       ├── chain.py
│       └── facade.py
└── tests/
    ├── test_normalize.py
    ├── test_cache.py
    ├── test_rate_limit.py
    ├── test_budget.py
    ├── test_call_counter.py
    ├── test_credits.py
    ├── test_ddg.py
    ├── test_providers_duckduckgo.py
    ├── test_providers_tavily.py
    ├── test_providers_firecrawl.py
    ├── test_router.py
    ├── test_chain.py
    ├── test_server.py
    └── test_server_integration.py
```

## 3. Build phases

### Phase A — Skeleton (orchestrator thread; ~30 min equivalent)

Goal: empty but importable; FastMCP starts and exposes `/health` and `server/discover` correctly.

Files owned by orchestrator:

- `pyproject.toml`, `.env.example`, `.gitignore`, `AGENTS.md`
- `src/free_web_search_mcp/{__init__,__main__,server,settings,normalize,cache,rate_limit,budget,ddg,prompts,resources,call_counter,credits}.py` as **stubs** (empty FastMCP app; tool/prompt/resource functions raise `NotImplementedError`)
- `src/free_web_search_mcp/providers/{__init__,base,duckduckgo,tavily,firecrawl,router,chain,facade}.py` as stubs
- `tests/test_server.py` with one smoke test (advertise + discover)

Acceptance: `pip install -e .` succeeds; `pytest -q tests/test_server.py` passes; `python -m free_web_search_mcp` starts and exits via SIGTERM cleanly.

### Phase B — Provider adapters (three sub-agents, parallel)

Three sub-agents, each owns **one** provider adapter plus its tests, with these inputs in common:

- The Provider ABC + exception taxonomy from `providers/base.py` (Phase A deliverable)
- The sibling repo to copy from

| Sub-agent | Owns | Source of truth | Output |
| --- | --- | --- | --- |
| `B-DDG` | `providers/duckduckgo.py`, `tests/test_ddg.py`, `tests/test_providers_duckduckgo.py` | `/home/cohorte8/Documents/ssr/projects/mcps/duck-search-mcp/src/duck_search_mcp/ddg.py`, `tests/test_ddg.py` | Adapter implementing `Provider.search()`; mapping tests inherited from sibling; new tests for the Provider API |
| `B-Tavily` | `providers/tavily.py`, `tests/test_providers_tavily.py` | `/tmp/opencode/repos/tavily-search-mcp/src/tavily_search_mcp/tavily.py`, `tests/test_tavily.py` | Adapter exposing `Provider.search()` and `Provider.fetch_urls()`; copy `TavilyClient` and mappers |
| `B-Firecrawl` | `providers/firecrawl.py`, `tests/test_providers_firecrawl.py` | `/tmp/opencode/repos/fireclaw-search-mcp/src/fireclaw_search_mcp/firecrawl.py`, `tests/test_firecrawl.py` | Adapter exposing `Provider.search()`, `Provider.fetch_url()`; copy `FirecrawlClient` and mappers + `CreditTracker` |

Each sub-agent's contract:

1. **Do not invent API.** If a method or constant in the sibling repo's client is unclear, copy it verbatim. If copying is impossible (e.g. sibling file is missing), say so in the agent's `## Output` block — do not guess.
2. **Match the Provider ABC.** `search(query, count_or_max_results, force_refresh)` returns the SPEC §3 response shape mapped to the Provider-neutral envelope. Same for `fetch_url` and `fetch_urls`.
3. **Own its own cache namespace and rate-limit bucket.** The adapter pulls the right namespace out of `Settings` and registers itself with the shared `DiskCache` and `MinIntervalLimiter`.
4. **Update `Budget`.** Tavily adapter increments the shared `CallCounter`. Firecrawl adapter hydrates/changes the shared `CreditTracker`. DDG adapter is read-only against `Budget`.
5. **Tests pass hermetically** (no network). Run `pytest -q tests/test_providers_<name>.py` before reporting.

### Phase C — Routing, chain, facade (one sub-agent)

Goal: the Strategy + Chain + Facade live. Server.py still has stubs.

Files:

- `providers/router.py` — `pick(intent: Intent) -> Provider | None`; decision matrix per SPEC §6.
- `providers/chain.py` — `try_chain(picked: Provider | None, intent: Intent, ...) -> tuple[Response, list[Fallthrough]]`; engages on Auth/Quota/RateLimit only; surfaces transport errors immediately.
- `providers/facade.py` — `orchestrate(intent: Intent, ...) -> Response`; thin glue; populates `provider_used`, `fallthroughs[]`, runtime fields.
- `tests/test_router.py`, `tests/test_chain.py` — `FakeProvider` used to drive the matrix without real upstreams.

Sub-agent input: the three Provider adapters from Phase B; SPEC §6 verbatim.

### Phase D — Tools (one sub-agent)

Goal: server.py tool functions are real; the FastMCP app advertises them with the schemas and descriptions in SPEC §3.

Files:

- `src/free_web_search_mcp/server.py` — replace the six tool stubs with real implementations calling `providers.facade.orchestrate`.
- `src/free_web_search_mcp/providers/__init__.py` — `build_all(settings)` factory; called from lifespan.
- `src/free_web_search_mcp/server.py` lifespan — wires cache, rate_limiters, budget, providers, http_client.
- `tests/test_server.py` — extend with per-tool flow tests; switch on `mode="auto"` and `mode="legacy"` to satisfy FastMCP 4 dual-era.
- `tests/test_server_integration.py` — stubs that skip unless `FWS_NETWORK_TESTS=1`.

Sub-agent input: SPEC §3 + §10; Phase C facade.

### Phase E — Prompts, resources, skills, docs (four sub-agents, parallel)

| Sub-agent | Owns |
| --- | --- |
| `E-prompts` | `prompts.py` (`route_query`, `research_brief`, `audit_budget`) + body text in SPEC §5. |
| `E-resources` | `resources.py` (`providers://status`, `providers://budget`, `providers://schema`, `cache://stats`). Verify the FastMCP 4 `@mcp.resource` signature against `gofastmcp.com/servers/resources` before writing; return `dict` and rely on FastMCP's auto-JSON inference plus `mime_type="application/json"`. |
| `E-skills` | `.agents/skills/web-search-routing/SKILL.md` and `.agents/skills/web-search-budgeting/SKILL.md`. Author from scratch. Pull the routing rules from SPEC §6 verbatim; pull the budget semantics from SPEC §8. Frontmatter style and depth should mirror the sibling repos' reference skill (`mcp-fastmcp-2026`). |
| `E-docs` | `README.md` (Quick start; description of what's inside; pointer to SPEC.md and DEPLOYMENT.md), `docs/DEPLOYMENT.md` (mirror of sibling DEPLOYMENT.md, adapted for the orchestrator's URL/path), `docs/CHANGELOG.md` (single v0.1 entry). |

Sub-agent `E-resources` must hand back a working `resources/list` and `resources/read` for each URI; orchestrator runs the wire probe (`probe_mcp_server.py`) on the live server after this phase.

Orchestrator thread also copies `.agents/skills/mcp-fastmcp-2026/` from `/home/cohorte8/Documents/ssr/projects/mcps/duck-search-mcp/.agents/skills/mcp-fastmcp-2026/` at this phase.

### Phase F — Verification + deployment (orchestrator thread)

Verification gates, in order, each must pass:

1. `pip install -e .` clean.
2. `pytest -q` runs all test modules, both `mode="auto"` and `mode="legacy"`, with 0 failures. Tests that need real network (only `test_server_integration.py`) remain skipped unless `FWS_NETWORK_TESTS=1`.
3. `python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .` exits 0.
4. Start the server: `python -m free_web_search_mcp &`. Wait for `Uvicorn running on http://127.0.0.1:8000`.
5. `python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp` exits 0.
6. `curl -s http://127.0.0.1:8000/health` returns `{"status":"ok"}`.
7. `curl` the discover endpoint with the headers from SPEC §10 and confirm `supportedVersions: ["2026-07-28"]` and the tool/resource/prompt lists match SPEC.
8. Stop the server, archive the cache dir, commit nothing yet.

Deployment: `python -m free_web_search_mcp` runs in the background; we expose it via **VS Code "Forward a Port"** on port 8000 with **Visibility=Public** so the tunnel URL is reachable by anyone with the link (the criterion says "cualquier cliente"). Capture the printed `https://vscode-<...>.devtunnels.ms/mcp` URL.

Email payload (assemble, do not send): the tunnel URL, the path of `web-search-routing/SKILL.md` and `web-search-budgeting/SKILL.md`, and a one-line summary of what the orchestrator exposes (6 tools / 4 resources / 3 prompts across DDG / Tavily / Firecrawl). The user authorises sending.

## 4. Sub-agent ownership map

| Sub-agent | Phase | Worktree | Inputs | Deliverable |
| --- | --- | --- | --- | --- |
| `B-DDG`, `B-Tavily`, `B-Firecrawl` | B (parallel) | orchestrator's working dir (split via non-overlapping file paths) | SPEC §2/§3 + sibling repo path + Phase A `base.py` | Provider adapter + Provider-level tests, all green |
| `C-router-chain-facade` | C | orchestrator's dir | Phase B adapters + SPEC §6 | router.py / chain.py / facade.py + tests |
| `D-tools` | D | orchestrator's dir | Phase C facade + SPEC §3 | server.py real tool bodies; both-mode tests |
| `E-prompts`, `E-resources`, `E-skills`, `E-docs` | E (parallel) | orchestrator's dir | SPEC §4 / §5 / §7 | prompts.py / resources.py / SKILL.md files / README + DEPLOYMENT + CHANGELOG |
| Orchestrator (this thread) | A, F | n/a | n/a | skeleton, integration, verification, deployment |

Worktree discipline: each sub-agent receives a list of **files it owns** and **files it must not edit**. Conflict surfaces (e.g. server.py imports tools; Phase D owns server.py; Phase E owns resources.py that registers on the FastMCP instance in server.py) are resolved by **registration happens in server.py only** — Phase E sub-agents return ready functions and Phase D wires them onto the FastMCP instance.

## 5. Verification gates (mapped to SPEC §15)

| # | Criterion | Evidence collected |
| --- | --- | --- |
| 1 | ≥3 resources | `server/discover` JSON resource list contains `providers://status`, `providers://budget`, `providers://schema`, `cache://stats` (4). Captured by Phase F step 7. |
| 2 | ≥5 tools, ≥2 cloud APIs | `tools/list` shows 6 tool names; `pro_search` carries `provider_used: "tavily"` or `"firecrawl"`; `deep_search` carries `provider_used: "firecrawl"` or `"tavily"`. Captured by Phase F step 7. |
| 3 | ≥2 prompts consuming the APIs | `prompts/list` shows `route_query`, `research_brief`, `audit_budget` (3); their bodies call `pro_search` / `deep_search` / `credit_status`. Captured by Phase F step 7. |
| 4 | Deployable, reachable from any client | Tunnel URL live by Phase F step 8; `curl server/discover` over the tunnel returns the expected discovery shape. |
| 5 | No auth | No `JWTVerifier`, no `api_key` checks on the wire; FastMCP constructor is plain. Probe flags this as the design. |
| 6 | Send link + ≥1 skill | Email payload in §3 Phase F has the tunnel URL + pointers to `web-search-routing/SKILL.md` and `web-search-budgeting/SKILL.md`. |
| 7 | Code + docs | `docs/{SPECS,PLAN,DEPLOYMENT,CHANGELOG}.md`, `README.md`, `AGENTS.md`. Source tree matches §2 here. |

## 6. Risks and known unknowns

- **Firecrawl & Tavily upstream shape drift.** The sibling repos were the source of truth for the API shape; if either upstream has shifted since the siblings were last touched, Phase B catches it. Mitigation: every adapter inherits a `*_map_*` test fixture from the sibling repo's tests; if the upstream moves, the test fails loudly and we fix the mapper.
- **FastMCP 4.0.11 resource decorator differs from the docs.** Resources doc was fetched from `gofastmcp.com/servers/resources` at SPEC time and the API surface (URI, name, description, mime_type, meta, annotations) matches. The orchestrator runs Phase F's wire probe on `resources/read` for each registered URI to catch any divergence at runtime.
- **Tavily call-counter drift.** Two orchestrator processes against one Tavily key drift independently. Single-process only; SPEC §15 calls this out.
- **`.env` is gitignored.** The orchestrator doesn't ship secrets. Per AGENTS.md, when the orchestrator hits Phase F step 8, the user fills in `.env` themselves with `TAVILY_API_KEY` and/or `FIRECRAWL_API_KEY`. We never write the secret value to disk from this session.
- **The `cache://stats` resource.** Telemetry-only; requires hit/miss counters on `DiskCache`. If a refactor pushes that out of scope, drop the resource (`providers://{status,budget,schema}` still satisfies the ≥3 criterion) and update SPEC §4.

## 7. Out of scope (mirrors SPEC §17)

No microservices; no streaming; no MRTR confirms; no auth; no auto-routing based on `recommendation` (advisory only); no async Firecrawl polling endpoints; no parallel upstream calls per tool.

## 8. Reporting cadence

- After Phase A: report skeleton import + smoke test green.
- After Phase B (all three): report per-provider adapter green; quote any sibling-shape deltas.
- After Phase C: report router+chain matrix green.
- After Phase D: report server.py integration green in both modes.
- After Phase E: report `tools/list`, `resources/list`, `prompts/list` match SPEC.
- After Phase F: report full verification log + tunnel URL + ready-to-send email payload.
