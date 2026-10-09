# `free-web-search-mcp` — MCP server

> **Status:** v0.1 in design. Speaks **MCP 2026-07-28** over Streamable HTTP and stdio, built on **FastMCP 4.0.11**. Aggregates three independent upstreams — DuckDuckGo Instant Answer (`api.duckduckgo.com`, free, no key), Tavily v1 (`api.tavily.com`, Bearer, in-process call counter, signup tier 1,000 credits/month, no live remaining-credit endpoint), Firecrawl v2 (`api.firecrawl.dev/v2`, Bearer, live `/v2/team/credit-usage` probe, signup tier 1,000 credits/month) — behind a single, cacheable MCP surface. No authentication on the server; loopback bind by design; expose via VS Code port forwarding.

| Provider | Backend | Cost | Coverage |
| --- | --- | --- | --- |
| `duckduckgo` | `api.duckduckgo.com/?q=…&format=json&t=…` | Free, unauthenticated | Entity-shaped queries (people, places, products, standards, calculations). Short snippets. |
| `tavily` | `POST api.tavily.com/search`, `POST api.tavily.com/extract` | Bearer, ~1k credits/mo | Question-shaped / long-tail web search; batch URL extraction (1–20 URLs). LLM-ready snippets with optional `raw_content`. |
| `firecrawl` | `POST api.firecrawl.dev/v2/search`, `POST api.firecrawl.dev/v2/scrape`, `GET api.firecrawl.dev/v2/team/credit-usage` | Bearer, ~1k credits/mo | Deep web search with full-page clean markdown; single-URL scrape; live credit balance. |

| Tool | Backend | Maps from sibling |
| --- | --- | --- |
| `entity_search` | DuckDuckGo Instant Answer | `duck-search-mcp::web_search` (renamed) |
| `pro_search` | Tavily `/search` | `tavily-search-mcp::web_search` (renamed) |
| `deep_search` | Firecrawl `/v2/search` | `fireclaw-search-mcp::web_search` (renamed) |
| `scrape_url` | Firecrawl `/v2/scrape` | `fireclaw-search-mcp::scrape_url` |
| `extract_urls` | Tavily `/extract` | `tavily-search-mcp::extract_url` (renamed) |
| `credit_status` | Merged view (Tavily counter + Firecrawl probe + DDG flag) | New — aggregates all three providers. |

| Prompt | Use |
| --- | --- |
| `route_query` | Given a topic, return a plan that tells the model which orchestrator tool to call and why, including the empty-result and budget-aware fallback rules. |
| `research_brief` | Multi-step plan: budget audit, route, execute, re-route on empty or failure. |
| `audit_budget` | Read-only plan that calls `credit_status` and `providers://budget`, then narrates the recommended mode for the rest of the session. |

| Resource | URI | Use |
| --- | --- | --- |
| `providers://status` | `providers://status` | Per-provider reachability, last-error, configured-flag, last-call timestamp. |
| `providers://budget` | `providers://budget` | Same shape as `credit_status()`'s response, surfaced as a resource so it is cacheable and addressable. |
| `providers://schema` | `providers://schema` | Combined JSON-Schema for every tool, resource template, and prompt — lets an agent introspect without external docs. |
| `cache://stats` | `cache://stats` | Optional (counted toward the ≥3 requirement on its own). Per-provider cache hit/miss counters and directory size. |

---

## 1. Why this server exists

The three sibling MCPs (`duck-search-mcp`, `tavily-search-mcp`, `fireclaw-search-mcp`) were designed as a tripod: each ships a single shape — entity snippets, general snippets, deep markdown — and every prompt in each sibling literally tells the model to "switch to a free sibling search tool" when its own budget is tight. `free-web-search-mcp` is the seat that tripod sits on. It does not replace the siblings (each stays standalone-deployable); it gives an agent a single connection with the union surface and explicit rules for which provider to reach for in which situation.

The orchestrator's three "free-first" choices are deliberate:

- DDG is genuinely free and entity-shaped; it always works; budget signal is meaningless.
- Tavily and Firecrawl are paid and offer different trade-offs (Tavily is cheap and LLM-friendly for general snippets; Firecrawl is expensive and full-page for deep retrieval). They are different cloud services and intentionally both included because the acceptance criteria require ≥2 distinct cloud APIs.
- Failure of one paid provider (Auth, Quota, RateLimit) falls through to the **other paid provider of the same shape**, never silently back to DDG, because DDG has a different surface and a silent shape change would be worse than a clear error.

## 2. Architecture

Modular monolith with a single FastMCP process and a layered, per-provider adapter pattern. Single deployment, single health endpoint, single tunnel — one `/mcp` URL for any client.

```
free-web-search-mcp/
├── AGENTS.md, README.md, .env.example, .gitignore, pyproject.toml
├── .agents/skills/
│   ├── mcp-fastmcp-2026/            # reference skill, copied from siblings
│   ├── web-search-routing/SKILL.md  # agent skill shipped with the server
│   └── web-search-budgeting/SKILL.md
├── docs/{SPECS, PLAN, DEPLOYMENT, CHANGELOG}.md
├── src/free_web_search_mcp/
│   ├── __main__.py          # python -m free_web_search_mcp entry point
│   ├── server.py            # FastMCP root: lifespan, INSTRUCTIONS, /health, tool/prompt/resource registration
│   ├── settings.py          # FWS_* env vars + per-provider credential resolution
│   ├── normalize.py         # query/url normalisation, slug, FNV-1a 64
│   ├── cache.py             # DiskCache with per-provider key namespace
│   ├── rate_limit.py        # MinIntervalLimiter (per-provider instance)
│   ├── budget.py            # merged Budget: Tavily CallCounter + Firecrawl CreditTracker + DDG flag
│   ├── providers/
│   │   ├── __init__.py      # PROVIDERS registry, build_all(settings) factory
│   │   ├── base.py          # Provider ABC: search/fetch/status
│   │   ├── duckduckgo.py    # Adapter (Strategy + Template Method subclass)
│   │   ├── tavily.py        # Adapter
│   │   ├── firecrawl.py     # Adapter
│   │   ├── router.py        # Strategy: pick(intent) -> Provider
│   │   ├── chain.py         # Chain of Responsibility: try_chain(picked, ...)
│   │   └── facade.py        # Facade: orchestrate() — single entry from server.py
│   ├── prompts.py           # route_query, research_brief, audit_budget
│   ├── resources.py         # providers://{status,budget,schema}, cache://stats
│   └── ddg.py               # DDG-specific mapping (extracted from sibling for clarity)
└── tests/
    ├── test_normalize.py, test_cache.py, test_rate_limit.py
    ├── test_router.py, test_chain.py
    ├── test_providers_{duckduckgo,tavily,firecrawl}.py
    ├── test_budget.py
    ├── test_server.py
    └── test_server_integration.py
```

Layering, top to bottom:

| Layer | Responsibility |
| --- | --- |
| **MCP surface** (`server.py`, `prompts.py`, `resources.py`) | Receives tool/prompt/resource calls; reads shared state from `ctx.request_context.lifespan_context`. |
| **Facading** (`providers/facade.py`) | Single `orchestrate(intent, args)` entry. Calls the router, then the chain. Returns the merged response with `provider_used` and `fallthroughs[]`. |
| **Routing & Chain** (`providers/router.py`, `providers/chain.py`) | Strategy picks the primary provider from intent; Chain runs the primary and decides whether (and to whom) to fall through. |
| **Provider adapters** (`providers/{duckduckgo,tavily,firecrawl}.py`) | One per upstream. Each owns its HTTP client, cache subset, rate-limit bucket, and budget contribution. Adapts the upstream shape to `Provider` methods. |
| **Budget & Cache** (`budget.py`, `cache.py`) | Cross-provider state: budget merge, per-provider cache key namespace, per-provider rate limiter. |

Lifespan owns **all** state via `lifespan_context`, per FastMCP 4's stateless rule (no module-level globals keyed by connection; the server is stateless across replicas in principle even though we run single-process for v0.1).

State (read-only view of what lives inside the lifespan):

- `settings: Settings` — frozen dataclass built once at startup from env.
- `cache: DiskCache` — namespaced by provider; the `provider` parameter is part of the cache key.
- `rate_limiters: dict[ProviderName, MinIntervalLimiter]` — one per provider, independent buckets.
- `budget: Budget` — merged Tavily counter + Firecrawl tracker + DDG flag.
- `providers: dict[ProviderName, Provider]` — only the providers whose credentials are configured.
- `http_client: httpx.AsyncClient` — single shared client owned by the lifespan; attached to any provider that needs it.
- `last_call_unix: dict[ProviderName, float]` — surfaced through `providers://status`.

## 3. Tool surface

The orchestrator exposes **six** tools. All tools return `structured_content` per FastMCP 4. All tool descriptions are stable strings the model reads verbatim — keep them in sync with this doc.

### 3.1 `entity_search` — DuckDuckGo

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "query":         { "type": "string",  "minLength": 1 },
    "count":         { "type": "integer", "default": 5 },
    "force_refresh": { "type": "boolean", "default": false }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "query": "Rust programming language",
  "provider_used": "duckduckgo",
  "fallthroughs": [],
  "results": [
    { "title": "Rust (programming language) - Wikipedia",
      "url":   "https://en.wikipedia.org/wiki/Rust_(programming_language)",
      "snippet": "..." }
  ],
  "source": "upstream" | "cache",
  "note": "" | "DuckDuckGo returned no results — rephrase once as a canonical topic name, then answer from your own knowledge."
}
```

**Description (the model reads verbatim):**

> Free entity-shaped web search via the DuckDuckGo Instant Answer API. Reach for this first for canonical names: people, places, products, standards, programming languages, calculations. Returns short snippets and no page bodies. Long-tail or question-shaped queries often return an empty response — that's a `200` with `results: []` and a guidance `note`, NOT an error; rephrase once as a canonical topic name, then fall back to your own knowledge. This tool never falls through to another provider (DDG has no Auth, Quota, or RateLimit path that warrants a fallback in this orchestrator).

**Counts:** `count` clamped to `[1, 10]`. Cache key = `("duckduckgo", normalised query)`.

### 3.2 `pro_search` — Tavily

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "query":         { "type": "string",  "minLength": 1 },
    "max_results":   { "type": "integer", "default": 5 },
    "force_refresh": { "type": "boolean", "default": false },
    "depth":         { "type": "string",  "enum": ["basic", "advanced"], "default": "basic" }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "query": "FastMCP lifespan context",
  "provider_used": "tavily" | "firecrawl",
  "fallthroughs": [
    { "from": "tavily", "to": "firecrawl", "reason": "TavilyQuotaError" }
  ],
  "answer": "...",
  "results": [
    { "title": "...", "url": "...", "content": "...",
      "score": 0.92, "raw_content": "...", "raw_content_chars": 12000,
      "raw_content_truncated_chars": 8000, "truncated": true,
      "favicon": "...", "id": "..." }
  ],
  "request_id": "...",
  "cache": "miss" | "hit" | "bypassed",
  "note": "",
  "usage": { "provider": "tavily", "calls_this_session": 17,
             "billing_period_end": null, "note": "Tavily does not expose a remaining-credit endpoint; the counter is local." }
}
```

**Description:**

> Question-shaped general web search via Tavily. Reach for this when the prompt is a question, a long-tail lookup, or anything that wants LLM-ready snippets (not page bodies). Tavily charges credits per call (signup tier ~1,000/month; `depth="advanced"` = 2 credits, `depth="basic"` = 1). Every response includes `usage.calls_this_session` so the model can self-budget. On `Auth`, `Quota`, or `RateLimit` errors only, the orchestrator falls through to `deep_search` (Firecrawl) — empty results DO NOT trigger fall-through; rephrase and retry. Per-result `raw_content` is capped at ~8k chars; the cut is visible via `truncated: true`. The `firecrawl_*` enum is reserved for the `deep_search` tool; do not call this tool expecting markdown page bodies.

**Counts:** `max_results` clamped to `[1, 10]`. Cache key = `("tavily", normalised query, depth)`.

### 3.3 `deep_search` — Firecrawl

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "query":           { "type": "string",  "minLength": 1 },
    "max_results":     { "type": "integer", "default": 5 },
    "force_refresh":   { "type": "boolean", "default": false },
    "include_markdown":{ "type": "boolean", "default": true }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "query": "how to configure a custom JSON logger in tokio",
  "provider_used": "firecrawl" | "tavily",
  "fallthroughs": [
    { "from": "firecrawl", "to": "tavily", "reason": "FirecrawlCreditsError" }
  ],
  "results": [
    { "title": "...", "url": "...", "description": "...",
      "markdown": "...", "markdown_chars": 24000,
      "markdown_truncated_chars": 8000, "truncated": true }
  ],
  "credits_used_this_call": 7,
  "usage": { "provider": "firecrawl", "remaining_credits": 812,
             "plan_credits": 1000, "billing_period_end": "2026-11-01T00:00:00+00:00",
             "source": "live" | "cached", "probe_note": "" },
  "cache": "miss" | "hit" | "bypassed",
  "note": ""
}
```

**Description:**

> Deep web search via Firecrawl. Reach for this when you need the **full page contents** as clean markdown — not just snippets — or when the prompt is complex enough that snippets won't cut it. Costs Firecrawl credits (free tier 1,000/month; a typical call uses 2–7 credits). Every response includes a `usage.remaining_credits` snapshot — switch to `pro_search` once it drops under ~50. Set `include_markdown=false` to skip per-result scraping (saves credits; you only get the description). On `Auth`, `Quota`, or `RateLimit` errors only, falls through to `pro_search` (Tavily). Per-result markdown is capped at ~8k chars; the `markdown_chars` / `markdown_truncated_chars` fields tell you how much was cut.

**Counts:** `max_results` clamped to `[1, 10]`. Cache key = `("firecrawl", normalised query, include_markdown)`.

### 3.4 `scrape_url` — Firecrawl

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "url":           { "type": "string", "format": "uri", "minLength": 1 },
    "force_refresh": { "type": "boolean", "default": false }
  },
  "required": ["url"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "url": "https://example.com/docs",
  "final_url": "https://example.com/docs",
  "title": "Example Docs",
  "markdown": "...",
  "markdown_chars": 12345,
  "markdown_truncated_chars": 8000,
  "truncated": true,
  "metadata": { "title": "...", "description": "...", "language": "en",
                "source_url": "...", "url": "...", "status_code": 200 },
  "credits_used_this_call": 1,
  "usage": { "provider": "firecrawl", "remaining_credits": 812,
             "plan_credits": 1000, "billing_period_end": "...", "source": "live" },
  "cache": "miss" | "hit" | "bypassed",
  "note": ""
}
```

**Description:**

> Scrape a single known URL via Firecrawl. Reach for this after a prior `pro_search` or `deep_search` pointed at the page, or when the user already gave you the URL. Costs 1+ credits. Returns the page body as clean markdown plus `metadata.title`, `metadata.description`, `metadata.language`, `metadata.url` (post-redirect). This tool does NOT fall through; there is no parallel single-URL extraction service in this set. If Firecrawl refuses the call, the model falls back to its own knowledge or summarises from the search-result snippet it already has.

### 3.5 `extract_urls` — Tavily

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "urls":          { "type": "array", "minItems": 1, "maxItems": 20,
                       "items": { "type": "string", "format": "uri" } },
    "force_refresh": { "type": "boolean", "default": false }
  },
  "required": ["urls"],
  "additionalProperties": false
}
```

**Output:**

```json
{
  "urls": ["https://a", "https://b"],
  "provider_used": "tavily",
  "fallthroughs": [],
  "results": [
    { "url": "https://a", "raw_content": "...",
      "raw_content_chars": 4000, "raw_content_truncated_chars": 4000,
      "truncated": false, "images": [...], "favicon": "..." }
  ],
  "failed_results": [ { "url": "https://b", "error": "..." } ],
  "request_id": "...",
  "cache": "miss" | "hit" | "bypassed",
  "note": "",
  "usage": { "provider": "tavily", "calls_this_session": 18,
             "billing_period_end": null, "note": "..." }
}
```

**Description:**

> Batch extract one or more URLs via Tavily (1–20 URLs in one call). Per-URL `raw_content` is capped at ~8k chars; truncation is visible via `truncated: true`. Failed URLs land in a separate `failed_results` array (an HTTP 200 can have entries in both `results` and `failed_results`). This tool does NOT fall through — single-URL extraction is Firecrawl's lane (`scrape_url`); batch extraction is Tavily's lane; there is no provider-to-provider swap on failure.

### 3.6 `credit_status` — merged budget view

**Input schema:** empty object (`additionalProperties: false`).

**Output:**

```json
{
  "providers": {
    "tavily":    { "configured": true,  "calls_this_session": 19,
                    "note": "Tavily does not expose a remaining-credit endpoint; the counter is local." },
    "firecrawl": { "configured": true,  "remaining_credits": 812,
                    "plan_credits": 1000, "billing_period_end": "2026-11-01T00:00:00+00:00",
                    "source": "live", "probe_note": "" },
    "duckduckgo":{ "configured": true,  "available": true,
                    "note": "Free, no key; no budget signal." }
  },
  "recommendation": {
    "entity_queries":        "entity_search",
    "general_questions":     "pro_search",
    "deep_retrieval":        "deep_search",
    "single_url":            "scrape_url",
    "batch_urls":            "extract_urls",
    "switch_to_free_when":   { "firecrawl_remaining_credits_below": 50,
                               "tavily_calls_this_session_above":  200 }
  },
  "called_at_unix": 1730000000.0
}
```

**Description:**

> Merged view of all three providers' budgets plus a routing recommendation computed from the rules in §6. Does NOT burn credits (Firecrawl's live probe is a metadata endpoint; Tavily's snapshot is in-process; DDG is free). Call this before a batch of expensive calls.

## 4. Resource surface

Resources are read-only views the agent can pull with `ctx.read_resource(uri)`. They must cache well (the FastMCP 4 list has `ttlMs`/`cacheScope`, and our tool list already advertises `cache_ttl=300, cache_scope="public"` at the server level — that cascades to resources).

### 4.1 `providers://status`

```json
{
  "tavily":    { "configured": true,  "last_call_unix": 1730000000.0,
                 "last_error": "", "consecutive_errors": 0 },
  "firecrawl": { "configured": true,  "last_call_unix": 1730000000.0,
                 "last_error": "FirecrawlCreditsError at 1730000000.0", "consecutive_errors": 1 },
  "duckduckgo":{ "configured": true,  "last_call_unix": 1730000000.0,
                 "last_error": "", "consecutive_errors": 0 }
}
```

Purpose: a status snapshot the model can pull when `providers://budget` is stale or doesn't tell it which providers are currently flapping. `consecutive_errors` is the simple moving-edge: it increments on every error the chain returns, resets to 0 on the next successful call.

### 4.2 `providers://budget`

Mirror of the `credit_status()` response (same shape, same contents, same `called_at_unix`). Surfaced as a resource so an agent can poll it cheaply without invoking the tool. `ttlMs` ≈ 60 s; `cacheScope` private (per-session freshness matters for budgets).

### 4.3 `providers://schema`

```json
{
  "tools":     [ { "name": "entity_search", "input_schema": {...}, "output_schema": {...},
                    "description": "..." }, ... ],
  "resources": [ { "uri": "providers://status", "content_type": "application/json",
                    "response_example": {...} }, ... ],
  "prompts":   [ { "name": "route_query", "arguments": [ {"name": "topic", "type": "string"} ],
                    "body": "..." }, ... ]
}
```

Purpose: one resource that has every shape the server declares, so the agent doesn't need an external doc. `cacheScope` public; `ttlMs` 3,600,000 (1 h).

### 4.4 `cache://stats`

```json
{
  "tavily":     { "hits": 14, "misses": 6, "dir_size_bytes": 524288 },
  "firecrawl":  { "hits": 9,  "misses": 3, "dir_size_bytes": 786432 },
  "duckduckgo": { "hits": 22, "misses": 5, "dir_size_bytes": 262144 },
  "totals":     { "hits": 45, "misses": 14, "dir_size_bytes": 1572864,
                  "last_purge_unix": 0 }
}
```

Purpose: operator telemetry. Optional but cheap; counted toward the ≥3 resource requirement for the acceptance criteria.

## 5. Prompt surface

### 5.1 `route_query(topic: str)`

A user-message prompt that returns a plan telling the model which tool to call. Body (excerpt — the full string lives in `src/free_web_search_mcp/prompts.py`):

```
You are about to research the topic '{topic}'.
1. Call providers://budget. Read `providers.tavilly.calls_this_session` and `providers.firecrawl.remaining_credits`. If Firecrawl is below 50 credits, prefer the free `entity_search` and `pro_search` paths.
2. Pick the tool: canonical entity name (person, place, product, standard) → `entity_search`; question-shaped / long-tail / general → `pro_search`; needs full page body or complex queries → `deep_search`.
3. Call the chosen tool with a canonical topic name (NOT a question). Pass the recommended defaults (count=5 / max_results=5).
4. If the result list is empty (`results: []` and `note` non-empty), rephrase once as a canonical topic name and call the same tool again.
5. If the tool returned `provider_used` different from what you called (i.e. the orchestrator fell through), the empty-result rule still applies — rephrase once on the new provider.
6. If the chosen tool raised a `ToolError`, STOP and audit_budget before retrying. Do not silently chain retry — the orchestrator already tried the chain.
7. Quote sparingly. Cite only URLs the tool actually returned.
```

### 5.2 `research_brief(topic: str)`

Multi-step plan for substantive research. Body (excerpt):

```
Research the topic '{topic}' thoroughly.
1. Run `audit_budget` to learn the budget posture for the session.
2. Run `route_query('{topic}')` to get the tool selection plan.
3. Execute the chosen tool. If the result is empty, follow `route_query` step 4 (rephrase once).
4. If `route_query`'s chosen tool raised a `ToolError` (e.g. rate-limited), re-call `route_query` with the narrowed scope (e.g. one specific URL candidate) so the planner can pick `scrape_url` or `extract_urls` instead.
5. If you still don't have an answer after two tool calls, answer from your own knowledge and clearly state that the search returned no results.
6. Cite sparingly. Every URL you cite must appear in a tool response.
```

### 5.3 `audit_budget()`

Read-only session-level audit. Body (excerpt):

```
Audit the search budget for this session.
1. Call `credit_status`. Capture `providers.tavily.calls_this_session`, `providers.firecrawl.remaining_credits`, `providers.firecrawl.billing_period_end`, and the `recommendation` block.
2. Surface a 3-line summary: (a) free tool posture, (b) Tavily calls so far, (c) Firecrawl remaining vs. plan.
3. Recommend a mode for the rest of the session: 'free-only' (Firecrawl below 50 AND Tavily above 200 calls), 'paid-mixed' (default), 'deep-only' (Firecrawl healthy AND the topic is complex enough to need it).
4. If the recommendation says 'free-only', prefer `entity_search` for any entity-shaped lookup until the next audit.
```

## 6. Routing and failure policy

The routing and chain decisions are encoded once, in `providers/router.py` and `providers/chain.py`, and are exercised through `providers/facade.py::orchestrate()`. Server.py's tool functions are thin wrappers around `orchestrate()`.

**Strategy (router.py::pick):**

| Caller intent | Primary provider | Notes |
| --- | --- | --- |
| `entity_search(query)` | `duckduckgo` | No fall-through (DDG has no Auth/Quota/RateLimit path that warrants a fallback). |
| `pro_search(query)` | `tavily` if configured else `firecrawl` if configured else error | Fall-through from tavily to firecrawl on Auth/Quota/RateLimit. |
| `deep_search(query)` | `firecrawl` if configured else `tavily` if configured else error | Fall-through from firecrawl to tavily on Auth/Quota/RateLimit (Tavily `pro_search` shape differs but the upstream `results[].content` is similar enough that the model can use it; we surface `provider_used: "tavily"` so the model can adapt). |
| `scrape_url(url)` | `firecrawl` only | No fall-through. |
| `extract_urls(urls)` | `tavily` only | No fall-through. |

**Chain (chain.py::try_chain):** the chain engages **only** on the following exceptions:

| Provider exception class | Why it triggers fall-through |
| --- | --- |
| `TavilyAuthError` / `TavilyQuotaError` / `TavilyRateLimitError` | Paid; the alternative paid provider is a legitimate substitute. |
| `FirecrawlAuthError` / `FirecrawlCreditsError` / `FirecrawlRateLimitError` | Paid; ditto. |

The chain does **not** engage on:

- Empty results (the model rephrases; providers stay honest about what they returned).
- A single-URL extraction failure of one URL inside `extract_urls` (`failed_results[]` carries the per-URL error).
- A network-level transient error (`httpx.ConnectError`, `httpx.TimeoutException`); these surface as `ToolError("... upstream transport error: ...")` so the orchestrator doesn't enter an infinite retry loop on a half-down provider.

`fallthroughs[]` in the response is an audit trail the model and the operator can read; it is populated by `chain.try_chain` whenever it actually swapped providers.

## 7. Caching

A single `DiskCache` instance, namespaced by provider. Cache key tuple shape:

- `("duckduckgo", normalised_query)`
- `("tavily", normalised_query, depth)`
- `("firecrawl", normalised_query, include_markdown)`
- `("tavily_extract", sorted_tuple_of_urls)`
- `("firecrawl_scrape", final_url)`

File names = `<provider>-<slug>-<fnv1a64>.json`. Failure semantics (missing / stale / corrupt / unwritable entry) match the siblings: a bad cache entry **never fails the call**, the tool logs `warn` and degrades to a plain uncached call. TTL is per-provider via `FWS_<PROVIDER>_CACHE_TTL_SECS`; default 24 h. `0` disables that provider's cache.

Cache stores the **full, untruncated** body so re-querying with a different `max_results` cap reuses the cached body and only re-truncates at presentation time (preserves operator-tunable `FWS_<PROVIDER>_MAX_*_CHARS`).

The `fallthroughs[]` audit trail and the live `usage` snapshot are **excluded** from the cached envelope; every call returns a fresh view of those.

## 8. Budget and rate limit

**Per-provider budget contributions:**

| Provider | In-process source | Surface in `credit_status()` |
| --- | --- | --- |
| `duckduckgo` | None (free, no key) | `{ "configured": true, "available": true, "note": "Free, no key; no budget signal." }` |
| `tavily` | `CallCounter` (counter increments on every successful /search or /extract) | `{ "configured": bool, "calls_this_session": int, "note": "Tavily does not expose a remaining-credit endpoint; the counter is local." }` |
| `firecrawl` | `CreditTracker` (`hydrate()` at startup probe; `charge(n)` after each successful call) | `{ "configured": bool, "remaining_credits": int?, "plan_credits": int?, "billing_period_end": str?, "source": "live"|"cached", "probe_note": str }` |

`budget.Budget` is the read-only merged view; it owns the `tavily` `CallCounter` and the `firecrawl` `CreditTracker` and exposes a thread-safe `.snapshot()` that `credit_status()` reads.

**Per-provider rate limit:** one `MinIntervalLimiter` per provider in `rate_limiters[provider]`; initialised with `FWS_<PROVIDER>_MIN_INTERVAL_MS`. `0` disables that provider's limiter.

**Budget-aware pre-checks:** the Firecrawl provider runs its `CreditTracker.guard(FWS_FIRECRAWL_MIN_CREDITS)` before every `web_search` (not on scrape — that's a known-number single call). The Tavily provider does a soft `guard(FWS_TAVILY_MAX_CALLS)`; the counter is in-process and counts only the current process's calls, so the guard is advisory. The DDG provider has no guard.

**Recommendation block:** computed at `credit_status()` time from these rules:

| Condition | `entity_queries` | `general_questions` | `deep_retrieval` | `single_url` | `batch_urls` |
| --- | --- | --- | --- | --- | --- |
| Always healthy | `entity_search` | `pro_search` | `deep_search` | `scrape_url` | `extract_urls` |
| Firecrawl `< 50` and Tavily `< 200` | `entity_search` | `pro_search` | `pro_search` (markdown comes from the `raw_content` field) | `scrape_url` (still works) | `extract_urls` |
| Firecrawl `< 50` and Tavily `>= 200` | `entity_search` | `entity_search` | `entity_search` | `scrape_url` (still works) | `extract_urls` |
| Tavily not configured, Firecrawl out of credits | `entity_search` | `entity_search` | `entity_search` | `scrape_url` (returns ToolError) | `extract_urls` (returns ToolError) |

The `recommendation` block is **advisory**, not enforced; the orchestrator's routing is fixed by §6, and `credit_status()` only changes what the model sees in `recommendation`. This avoids the orchestrator silently swapping providers mid-session.

## 9. Configuration

All configuration is via environment variables (per FastMCP 4 convention; nothing in code reads `.env` directly — `__main__.py` opts in to `python-dotenv` if installed, mirror of the Firecrawl sibling's pattern). Keys are namespaced `FWS_*` for orchestrator-level settings and `FWS_<PROVIDER>_*` for per-provider overrides.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TAVILY_API_KEY` | – | Optional. When absent, Tavily provider is not registered; `pro_search`/`extract_urls` return a `ToolError` referring to the orchestrator's `TAVILY_API_KEY` env. Required for `pro_search`/`extract_urls`. |
| `FIRECRAWL_API_KEY` | – | Optional. When absent, Firecrawl provider is not registered; `deep_search`/`scrape_url`/`credit_status().providers.firecrawl` all degrade. Required for `deep_search`/`scrape_url`. |
| `FWS_TRANSPORT` | `http` | `http` (Streamable HTTP) or `stdio`. |
| `FWS_HOST` | `127.0.0.1` | Loopback only by design. |
| `FWS_PORT` | `8000` | Bind port for HTTP. |
| `FWS_PATH` | `/mcp` | MCP endpoint path. |
| `FWS_CACHE_DIR` | `./cache/fws` | Cross-provider cache root. Per-provider subdirs are created on first write. |
| `FWS_HTTP_TIMEOUT_S` | `30.0` | Shared httpx client timeout for any provider that doesn't override it. |
| `FWS_LOG_LEVEL` | `WARNING` | Server-level logger; falls back to `MCP_LOG_LEVEL` then `FASTMCP_LOG_LEVEL` if unset. |
| `FWS_TAVILY_ENABLED` | `1` if `TAVILY_API_KEY` set else `0` | Force-enable / disable the Tavily adapter regardless of key presence. |
| `FWS_FIRECRAWL_ENABLED` | `1` if `FIRECRAWL_API_KEY` set else `0` | Same for Firecrawl. |
| `FWS_DDG_ENABLED` | `1` | Disable to remove the free tool (for paid-only deployments). |
| `FWS_DDG_CACHE_TTL_SECS` | `86400` | DDG cache TTL. `0` disables. |
| `FWS_DDG_MIN_INTERVAL_MS` | `1100` | DDG minimum spacing. |
| `FWS_TAVILY_CACHE_TTL_SECS` | `86400` | Tavily cache TTL. |
| `FWS_TAVILY_MIN_INTERVAL_MS` | `200` | Tavily min spacing. |
| `FWS_TAVILY_MAX_CALLS` | `200` | Advisory; `pro_search`/`extract_urls` raise a `ToolError` if the in-process counter is above this. `0` disables. |
| `FWS_TAVILY_MAX_RAW_CHARS` | `8000` | Per-result `raw_content` cap. |
| `FWS_FIRECRAWL_CACHE_TTL_SECS` | `86400` | Firecrawl cache TTL. |
| `FWS_FIRECRAWL_MIN_INTERVAL_MS` | `0` | Firecrawl min spacing (Firecrawl servers typically don't enforce; default 0). |
| `FWS_FIRECRAWL_MIN_CREDITS` | `50` | Firecrawl pre-call guard; raises `ToolError("Firecrawl has only N credits left…")` when the local counter is below this. |
| `FWS_FIRECRAWL_MAX_MARKDOWN_CHARS` | `8000` | Per-result markdown cap. |

The orchestrator deliberately does **not** inherit the siblings' variable names (`DDG_*`, `TAVILY_*`, `FIRECRAWL_*`) for runtime config, with the **single** exception of the API-key env vars (the keys themselves are exposed to the orchestrator as `TAVILY_API_KEY` and `FIRECRAWL_API_KEY` so users can copy their existing keys verbatim into the orchestrator's `.env`).

## 10. Protocol contract

| Contract | Value |
| --- | --- |
| Protocol revision | `2026-07-28` (advertised via `server/discover`; dual-era accepts legacy `initialize`). |
| Transports | Streamable HTTP at `/mcp` (default) + stdio (opt-in via `FWS_TRANSPORT=stdio`). |
| Bind address | `127.0.0.1` (loopback; expose via VS Code port forwarding or ngrok — see `docs/DEPLOYMENT.md`). |
| Authentication | **None.** Loopback binding is the only access control; tunnel auth is the only network auth. |
| Required response fields | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers; tool/resource lists advertise `ttlMs`/`cacheScope`. |
| Server features | `tools` (six), `prompts` (three), `resources/templates` (four), `extensions: io.modelcontextprotocol/ui`. |

The `FastMCP(...)` constructor uses `cache_ttl=300, cache_scope="public"` at the server level (mirror of the siblings). Per-resource `ttlMs` and `cacheScope` are set explicitly in `resources.py` per §4 (budget = 60 s private; schema = 1 h public).

No `initialize` handlers, no `Mcp-Session-Id` reads, no `transport="sse"`, no `ctx.elicit()`/`ctx.sample()` (per FastMCP 4 rules). State lives in `lifespan_context`.

## 11. Install / setup

```bash
cd free-web-search-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
$EDITOR .env   # set TAVILY_API_KEY and/or FIRECRAWL_API_KEY if you have them
```

**Dependencies:** `fastmcp==4.0.11`, `httpx>=0.27`. Tested with Python 3.12, works with 3.10+.

## 12. Run by hand

```bash
python3 -m free_web_search_mcp
# Streamable HTTP on http://127.0.0.1:8000/mcp by default

FWS_TRANSPORT=stdio python3 -m free_web_search_mcp
# stdio for embedded clients
```

A blank stdin in stdio mode lets the server sit idle. Point any modern MCP client at the URL or spawn the process; see `docs/DEPLOYMENT.md` for connection recipes per client.

## 13. Deployment

Two supported paths, identical to the siblings:

1. **VS Code "Forward a Port"** (recommended). Open the Ports panel, forward `8000`, copy the URL, point the client at `<forwarded-url>/mcp`. The tunnel runs through Microsoft's relay; no public hostname that corporate firewalls can blanket-block.
2. **ngrok** (fallback). `ngrok http 8000`. Free-tier URLs are publicly enumerable.

For both, the server is started with `python3 -m free_web_search_mcp`. No auth on the server itself; loopback bind + tunnel auth is the access control.

## 14. Failure modes

| What goes wrong | What the orchestrator does | What the model sees |
| --- | --- | --- |
| `TAVILY_API_KEY` not set | Tavily provider not registered at startup. `pro_search`/`extract_urls` return `ToolError("TAVILY_API_KEY is not set; pro_search unavailable. Use entity_search for free, or set the key in .env.")`. | A clear, action-oriented error. |
| `FIRECRAWL_API_KEY` not set | Firecrawl provider not registered. `deep_search`/`scrape_url` return the matching ToolError. `credit_status().providers.firecrawl.configured` is `false`. | Same shape. |
| Tavily `Auth` (401) | `pro_search`/`extract_urls` chain into Firecrawl (for `pro_search`) or surface `ToolError` (for `extract_urls` — no second-stage batch provider). | `provider_used: "firecrawl"`, `fallthroughs[]` populated. |
| Tavily `Quota` (432/433) | Same chain as Auth. | Same. |
| Tavily `RateLimit` (429) | Same chain. | Same. |
| Firecrawl `Auth` (401) | `deep_search` chain into Tavily (`pro_search` shape, `provider_used: "tavily"`); `scrape_url` surface `ToolError`. | `provider_used: "tavily"`, `fallthroughs[]` populated. |
| Firecrawl `Credits` (402) | Same chain. | Same. |
| Firecrawl `RateLimit` (429) | Same chain. | Same. |
| DDG upstream empty 200 | `entity_search` returns `results: []` + the guidance `note`. Does NOT chain. | Empty result with rephrase hint. |
| DDG upstream non-2xx | `entity_search` raises `ToolError("Upstream HTTP N: …")`. Does NOT chain. | A clear HTTP error. |
| Cache missing/stale/corrupt/unwritable | Per-provider cache layer logs `warn`, falls through to uncached call. | Same shape as a normal call, source="upstream". |
| Transport timeout / connection error | `ToolError("… upstream transport error: …")`; chain does NOT engage on transport errors (avoid runaway retry). | A clear transport error. |
| Foreign `Origin` header | Accepted when bound to `127.0.0.1` (DNS rebinding not possible). The probe flags this as a WARN. | No client-visible difference. |
| Server didn't start (port in use, venv missing) | Process exits with a clear log line on stderr. | Client gets a connection error. No silent fallback. |

## 15. Acceptance criteria (mapped to the deliverable contract)

| # | Criterion | Implementation |
| --- | --- | --- |
| 1 | ≥3 resources | `providers://status`, `providers://budget`, `providers://schema`, `cache://stats`. 4. |
| 2 | ≥5 tools, ≥2 on different cloud APIs | `entity_search`, `pro_search`, `deep_search`, `scrape_url`, `extract_urls`, `credit_status`. 6 tools. The two paid clouds are `api.tavily.com` and `api.firecrawl.dev`. |
| 3 | ≥2 prompts consuming the internet APIs | `route_query(topic)`, `research_brief(topic)`, `audit_budget()`. 3. |
| 4 | Deployable, reachable from any client | Streamable HTTP at `127.0.0.1:8000/mcp`; port-forwarded via VS Code or ngrok; exposed to Claude Desktop, Cursor, opencode, VS Code Copilot Chat. |
| 5 | No auth on the server | Loopback bind + tunnel auth. No `JWTVerifier`, no API key check on the wire. |
| 6 | Send the link by email with at least one skill | The tunnel URL, plus `.agents/skills/web-search-routing/SKILL.md` (and `.agents/skills/web-search-budgeting/SKILL.md`). |
| 7 | Code + docs | `SPECS.md`, `PLAN.md`, `DEPLOYMENT.md`, `CHANGELOG.md`, `README.md`; tests in `tests/`; static audit + wire probe in `.agents/skills/mcp-fastmcp-2026/scripts/`. |

## 16. Test plan

Unit tests (hermetic, no network) live under `tests/`:

- `test_normalize.py` — query and URL normalisation, slug, FNV-1a 64-bit hash.
- `test_cache.py` — round-trip, normalisation equivalence, per-provider namespace, stale entry, corrupt file, unwritable dir, atomic write.
- `test_rate_limit.py` — disabled at 0 ms, serial waits spaced, concurrent waits queued, per-provider buckets independent.
- `test_router.py` — Strategy picks the right primary per intent; falls back when the primary is not configured.
- `test_chain.py` — Chain engages on `Auth`/`Quota`/`RateLimit` only; does not engage on `EmptyResults`; populates `fallthroughs[]`; surfaces `ToolError` on transport errors.
- `test_providers_duckduckgo.py` — DDG mapping, empty response, `note`, hard cap; cache read/write.
- `test_providers_tavily.py` — `/search` and `/extract` mapping, including `raw_content` truncation; `CallCounter` increments; `force_refresh` bypasses cache.
- `test_providers_firecrawl.py` — `/v2/search` and `/v2/scrape` mapping, including markdown truncation; `CreditTracker` hydrate / charge / guard; `force_refresh` bypasses cache.
- `test_budget.py` — merged snapshot is stable; `recommendation` block computes the right mode per the §8 table.
- `test_server.py` — tool/prompt/resource advertisement; basic call on each tool; empty-result shape; cache hit; tool-count clamps; resource reads. Run in both `mode="auto"` and `mode="legacy"` to satisfy FastMCP 4 dual-era.
- `test_server_integration.py` — skipped unless `FWS_NETWORK_TESTS=1`; covers real DDG / Tavily / Firecrawl calls.

Verification gates (in order, all must pass before delivery):

1. `python -m pytest` — 0 failures in both `mode="auto"` and `mode="legacy"`.
2. `python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .` — static audit clean (exit 0).
3. `python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp` — wire probe clean (exit 0).

End-to-end manual: start the server with at least one API key set, connect from opencode or Claude Desktop, run `audit_budget()`, then `route_query("Rust programming language")` → `entity_search("Rust programming language")`, and verify `provider_used`, `fallthroughs[]`, and `usage.*` fields appear in the response.

## 17. Out of scope for v0.1

- No parallel intra-tool upstream calls (each tool call is single-flight; a shared per-provider `MinIntervalLimiter` mutex serialises them).
- No additional upstream providers beyond DDG / Tavily / Firecrawl. Brave is excluded per its own terms of service.
- No automatic model-side budget grant: the credit counter is local to the process; two orchestrator processes sharing one API key drift independently. Acceptable for the single-process deployment this server targets.
- No async Firecrawl polling endpoints (`/v2/crawl`, `/v2/extract`, `/v2/deep-research`); only sync endpoints per the Firecrawl sibling's SPEC §16. Same for Tavily (no async research endpoint used).
- No streaming responses.
- No `InputRequiredResult` (MRTR) for confirm-before-acting; the orchestrator never performs a destructive action, so there is nothing to confirm.
- No authenticated deployments. The server is unauthenticated by design; loopback bind + tunnel is the only access control.
- No change-tracking (`ChangeTracking` format on Firecrawl scrape). Add as a future tool only if a real caller needs it.

## 18. References

- MCP specification (target revision `2026-07-28`): https://modelcontextprotocol.io/specification/2026-07-28/
- MCP `server/discover`: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/discover
- FastMCP 4 docs: https://gofastmcp.com
- FastMCP source: https://github.com/PrefectHQ/fastmcp
- Tavily API: https://docs.tavily.com/documentation/api-reference/endpoint/search and /extract
- Firecrawl API: https://docs.firecrawl.dev (search, scrape, credit-usage)
- DuckDuckGo Instant Answer API: https://duckduckgo.com/duckduckgo-help-pages/settings/params
- VS Code Streamable HTTP support: https://code.visualstudio.com/api/extension-guides/ai/mcp
- Sibling repos: `duck-search-mcp`, `tavily-search-mcp`, `fireclaw-search-mcp` — same account, same FastMCP 4 / MCP 2026-07-28 baseline.
