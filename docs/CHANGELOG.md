# Changelog

All notable changes to `free-web-search-mcp` are recorded here. The format is loosely [Keep a Changelog](https://keepachangelog.com/) with sections per change type, and the version numbers follow [Semantic Versioning](https://semver.org/) once we move past `0.x`. Until `1.0`, any `0.y` bump may contain breaking response-shape changes called out in the `Added` / `Changed` notes.

## [0.1.0] — initial release

### Added

- **Modular-monolith MCP server** built on FastMCP `4.0.11` against the MCP `2026-07-28` revision, addressing six tools, four resources, and three prompts over a single `/mcp` endpoint. Source layout follows `src/free_web_search_mcp/` with the per-provider code under `providers/` and the routing layer (Strategy + Chain + Facade) kept in dedicated modules for clarity (`providers/router.py`, `providers/chain.py`, `providers/facade.py`).
- **Six tools:** `entity_search` (free DuckDuckGo Instant Answer, entity-shaped snippets), `pro_search` (Tavily `/search`, question-shaped general web search with `basic`/`advanced` depth), `deep_search` (Firecrawl `/v2/search`, deep web with full-page markdown), `scrape_url` (Firecrawl `/v2/scrape`, single-URL scrape with metadata), `extract_urls` (Tavily `/extract`, batch URL extraction, 1–20 URLs per call), `credit_status` (merged budget view across all three providers with an advisory routing recommendation).
- **Four resources:** `providers://status` (per-provider reachability, last-error, consecutive-errors counter, last-call timestamp), `providers://budget` (mirror of `credit_status()` with `ttlMs≈60s` private cache), `providers://schema` (combined JSON-Schema for every tool, resource, and prompt for self-describing introspection; `ttlMs=1h` public cache), `cache://stats` (per-provider hit/miss counters and cache dir size; counted toward the SPEC's ≥3 resource requirement).
- **Three prompts:** `route_query(topic)` (tool selection plan that respects budget posture and rephrase-on-empty rules), `research_brief(topic)` (multi-step research plan that consumes the APIs), `audit_budget()` (read-only session-level budget review with a recommended mode for the rest of the session).
- **Provider layer with explicit patterns:** Strategy (`providers/router.py`) picks the primary provider per intent; Chain of Responsibility (`providers/chain.py`) swaps to the other paid provider on `Auth`, `Quota`, or `RateLimit` errors only, never on empty results, never on transport errors (avoid runaway retry on a half-down upstream); Facade (`providers/facade.py`) is the single entry point that populates `provider_used`, `fallthroughs[]`, and runtime usage fields.
- **Provider registry:** `providers/__init__.py::build_all()` returns only the providers whose credentials are configured, so missing keys cleanly degrade the surface (the affected tool returns a `ToolError` referring to the specific env var to set).
- **Per-provider adapter pattern:** one adapter per upstream (`providers/duckduckgo.py`, `providers/tavily.py`, `providers/firecrawl.py`), each owning its HTTP call, cache namespace, rate-limit bucket, and budget contribution; inherits and adapts the upstream-shape tests from each sibling repo.
- **Cross-provider state in `lifespan_context`:** `Settings` (frozen dataclass from env), `DiskCache` (single instance, per-provider key namespace), per-provider `MinIntervalLimiter` instances, merged `Budget` (Tavily `CallCounter` + Firecrawl `CreditTracker` + DDG flag), single shared `httpx.AsyncClient`. No module-level globals; FastMCP 4 stateless rule honoured.
- **Per-provider configuration:** every cache TTL, rate-limit interval, credit guard, content-truncation cap, and enabled-flag is env-driven (`FWS_TAVILY_MAX_CALLS`, `FWS_FIRECRAWL_MIN_CREDITS`, `FWS_*_CACHE_TTL_SECS`, etc.); per-provider overrides let operators tune one provider without retuning the others. See SPEC §9 and `.env.example`.
- **Two agent skills in `.agents/skills/`:** `web-search-routing/SKILL.md` (routing decision rules pulled verbatim from SPEC §6, including the empty-result and budget-aware fallback policies) and `web-search-budgeting/SKILL.md` (budget semantics and the `recommendation` block rule table from SPEC §8). The reference skill `mcp-fastmcp-2026/` is included unchanged for spec context.
- **Verification suite:** static audit (`audit_mcp_project.py`) and wire probe (`probe_mcp_server.py`) under `.agents/skills/mcp-fastmcp-2026/scripts/`. CI-style gate: `pytest` → audit → probe, all must exit 0 before delivery. Integration tests gated on `FWS_NETWORK_TESTS=1` to keep the default run hermetic.
- **Five extra docs/notes:** `README.md` (one-page quick start), `docs/SPECS.md` (the contract), `docs/PLAN.md` (build phases), `docs/DEPLOYMENT.md` (connection recipes per client), `docs/CHANGELOG.md` (this file); `AGENTS.md` with the lineamiento shipped to the repo.

### Changed

- None. v0.1 is the initial release.

### Deprecated

- None.

### Removed

- None.

### Fixed

- Removed the `@dataclass` decorator from `ToolBudgetError` in `src/free_web_search_mcp/credits.py`. The decorator prevented the standard `RuntimeError(message)` constructor pattern from accepting the `"Firecrawl has only N credits left…"`-style error messages produced by `CreditTracker.guard()`, which silently fell back to bare `ToolBudgetError()` instances with no context. Stripping the decorator restores message-arg support and keeps the existing raise sites (`raise ToolBudgetError(f"Firecrawl has only {remaining} credits left…")`) compiling and propagating the diagnostic message. (Reported by the Phase B-Firecrawl sub-agent during the initial integration of the Firecrawl `CreditTracker`.)

### Security

- **Loopback bind by default.** The server binds to `127.0.0.1` and is exposed only by an operator-chosen tunnel (VS Code port forwarding is recommended; ngrok is a documented fallback). The "no auth on the server" stance is intentional per SPEC §17; tunnel auth is the only network auth, and adding JWT / API key checks at the MCP wire is explicitly out of scope for v0.1.
- **Origin validation defaults to permissive while loopback-bound.** DNS rebinding is not possible against a loopback-only listener, so the server accepts foreign `Origin` headers; a startup `WARN` flags this as a non-issue only because of the bind address. Operators moving the server off loopback must set `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=true` (see DEPLOYMENT §5).
- **Upstream credentials in `Authorization: Bearer …` over HTTPS only.** Tavily (`api.tavily.com`) and Firecrawl (`api.firecrawl.dev/v2`) are called with `Authorization: Bearer ${TAVILY_API_KEY}` and `Authorization: Bearer ${FIRECRAWL_API_KEY}` respectively, on TLS upstream endpoints. Tokens never appear in the request body, query string, or `User-Agent`. Keys are resolved from `TAVILY_API_KEY` and `FIRECRAWL_API_KEY` so users can copy their existing provider credentials verbatim into the orchestrator's `.env`. Neither paid provider ever sees what the orchestrator's on-disk cache stores (cache is operator-local and gitignored under `FWS_CACHE_DIR`).
- **Per-result content caps enforced server-side.** Tavily `raw_content` and Firecrawl `markdown` are truncated to `FWS_TAVILY_MAX_RAW_CHARS` (default 8000) and `FWS_FIRECRAWL_MAX_MARKDOWN_CHARS` (default 8000) before they leave the orchestrator; the truncation is visible in the response (`truncated: true`, plus `raw_content_chars` / `markdown_chars` and the corresponding truncated-by counts).
- **URL scheme allow-list on fetch tools.** `scrape_url` and `extract_urls` accept `http(s)` only and reject `file://`, `ftp://`, `javascript:`, `data:`, and any other scheme before forwarding to Firecrawl or Tavily, preventing the orchestrator from being used as a forward proxy for non-HTTP targets.
