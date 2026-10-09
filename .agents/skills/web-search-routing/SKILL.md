---
name: web-search-routing
description: > Routing policy for `free-web-search-mcp` v0.1 — picks the right tool (`entity_search`, `pro_search`, `deep_search`, `scrape_url`, `extract_urls`, `credit_status`) given the orchestrator's three-provider surface (DuckDuckGo + Tavily + Firecrawl) and encodes the chain rules from SPEC §6: fall through only on Auth/Quota/RateLimit, never on transport errors, never silently to DDG. Use whenever the agent sees one of the orchestrator's six tools in its tool list, gets `provider_used`/`fallthroughs[]` back from a tool response, has an empty `results: []` payload, or needs to decide whether to plan around budget before a batch of calls. Cross-reference `web-search-budgeting` for the credit signal behind this routing.
version: 0.1.0
tags:
  - free-web-search-mcp
  - routing
  - chain-of-responsibility
  - tavily
  - firecrawl
  - duckduckgo
---

# Web search routing — `free-web-search-mcp` v0.1

## When to use this skill

Consult this skill the moment any orchestrator tool name shows up in the model's tool list or the user asks a question that needs a fresh web lookup. The default mode of failure here is silent fallback that changes the response shape; this skill exists to keep the signal honest. If the user's task mentions credit usage, budget posture, or "should I switch", see `web-search-budgeting` first — routing lives downstream of the budget decision.

## The orchestrator's tool surface

The server exposes six tools. Picking the right one is the whole job; everything else is accounting.

| Tool | Provider(s) | When to reach for it |
| --- | --- | --- |
| `entity_search` | `duckduckgo` | Canonical entity name (person, place, product, standard, language, calculation). Free, never falls through. |
| `pro_search` | `tavily` → `firecrawl` | Question-shaped, long-tail, general web lookup. LLM-ready snippets; not page bodies. |
| `deep_search` | `firecrawl` → `tavily` | Needs full page contents as clean markdown, or topic is complex enough that snippets won't cut it. |
| `scrape_url` | `firecrawl` | Have one URL, want clean markdown + metadata. No fall-through. |
| `extract_urls` | `tavily` | Have 1–20 URLs, want per-URL `raw_content`. No fall-through. |
| `credit_status` | merged (DDG + Tavily + Firecrawl) | Read budget posture; free to call, does not burn credits. |

## Routing decision tree

```
Is the topic a canonical entity name (person / place / product / standard / language)?
  └─ yes → entity_search            (free; DDG; never falls through)
  └─ no  ↓
Do you already have a URL?
  ├─ one URL  → scrape_url          (Firecrawl; no fall-through)
  └─ 1–20     → extract_urls        (Tavily;   no fall-through)
  └─ no URL   ↓
Do you need full page body as clean markdown, or is the topic complex?
  ├─ yes → deep_search              (Firecrawl primary; falls to Tavily on Auth/Quota/RateLimit)
  └─ no  → pro_search               (Tavily primary;   falls to Firecrawl on Auth/Quota/RateLimit)
Unsure about budget posture for a batch?
  → credit_status first             (read providers://budget; advisory only — does NOT change routing)
```

The four fall-through-eligible errors are identical across the two paid shapes: `Auth`, `Quota`, `RateLimit`. Everything else (`Timeout`, `ConnectError`, empty results, malformed payload) does NOT trigger fall-through. See the next section for the verbatim policy.

## Fall-through rules (verbatim from SPEC §6)

The chain in `src/free_web_search_mcp/providers/chain.py::try_chain` engages **only** on these exception classes:

| Provider | Trigger class | What it means |
| --- | --- | --- |
| `tavily` | `TavilyAuthError` | 401 from `api.tavily.com` |
| `tavily` | `TavilyQuotaError` | 432/433 — quota / credits exhausted |
| `tavily` | `TavilyRateLimitError` | 429 from Tavily |
| `firecrawl` | `FirecrawlAuthError` | 401 from `api.firecrawl.dev` |
| `firecrawl` | `FirecrawlCreditsError` | 402 — credits exhausted |
| `firecrawl` | `FirecrawlRateLimitError` | 429 from Firecrawl |

On these six errors only, `pro_search` falls through Tavily → Firecrawl, and `deep_search` falls through Firecrawl → Tavily. Every other failure mode surfaces to the model and **does not** swap providers:

- Empty results (`results: []` + guidance `note`) — rephrase once; providers stay honest about what they returned.
- A single-URL failure inside `extract_urls` — reported in `failed_results[]`, the rest of the batch still comes back.
- Transport errors (`httpx.ConnectError`, `httpx.TimeoutException`, `… upstream transport error: …`) — surface as `ToolError`; the chain does **not** engage (avoid an infinite retry loop on a half-down provider).

`fallthroughs[]` in the response is the audit trail. If the array is non-empty, the primary did not run; trust `provider_used`, not what you called.

## Empty-result behavior

When the picked tool returns `results: []` with a non-empty `note`, treat it as a strong signal that the query phrasing was wrong, not as "nothing exists on the internet". Rephrase once as a canonical topic name (no question mark, no leading verb) and call the same tool again. If the second call also returns empty, answer from your own knowledge and clearly state that the search returned no results.

Quote sparingly. Every URL you cite must appear in a tool response. Do NOT synthesise URLs from prior knowledge and claim the tool returned them — that breaks the trust contract with the orchestrator.

## Common mistakes to avoid

- **Don't call `deep_search` for a single canonical entity.** It wastes 2–7 Firecrawl credits; `entity_search` is free and faster. "What is the Rust programming language?" is a `entity_search` question.
- **Don't reach for `pro_search` to read a page body.** Tavily returns snippets by design; pages ≥~8k chars get truncated (`raw_content_truncated_chars`). If you need the body, the choice is `deep_search` (recommended) or `scrape_url` (you already have the URL).
- **Don't retry on a `ToolError`.** The orchestrator already attempted the chain. Audit the budget with `credit_status`, then re-plan.
- **Don't skip the rephrase.** Long-tail questions to `entity_search` ("why does the sky look blue") reliably return empty; the canonical topic name "Rayleigh scattering" returns a hit.
- **Don't confuse "empty result" with "fall-through".** Empty payload does NOT populate `fallthroughs[]`; the chain never ran. A populated `fallthroughs[]` means the primary provider was skipped.
- **Don't call `scrape_url` speculatively.** If you have a candidate URL but no evidence it's the right page, prefer `pro_search` with the query, then `scrape_url` the result the search picked.
- **Don't expect `pro_search` to surface Firecrawl's markdown field.** It surfaces Tavily's `content` / `raw_content`. The shapes are different on purpose; that's why `provider_used` is in every response.

## Per-tool response fields that matter

### `entity_search`

| Field | Why you read it |
| --- | --- |
| `provider_used` | Always `"duckduckgo"` — fallback is impossible by design. |
| `fallthroughs[]` | Always empty. If non-empty, you are looking at the wrong response. |
| `results[].snippet` | The DDG snippet, sometimes one sentence. |
| `note` | If non-empty + `results: []`, run the empty-result rule. |
| `source` | `"upstream"` or `"cache"`; cache hits still mean a real DDG response was stored. |

### `pro_search`

| Field | Why you read it |
| --- | --- |
| `provider_used` | `"tavily"` or `"firecrawl"` — read it first; the shape differs. |
| `fallthroughs[]` | Populated only on Auth/Quota/RateLimit; never on empty results. |
| `answer` | Tavily's pre-synthesised answer; treat as one snippet among many. |
| `results[].raw_content` / `raw_content_truncated_chars` / `truncated` | Use `truncated: true` to know you saw a cut, not the whole page. |
| `usage.calls_this_session` | Tavily counter; advisory only for routing, real for budgeting. |
| `usage.note` | Always says the counter is local — be honest with the user. |
| `cache` | `"hit"` means you didn't burn a credit; the response shape is still authoritative. |

### `deep_search`

| Field | Why you read it |
| --- | --- |
| `provider_used` | `"firecrawl"` or `"tavily"` — same shape as `pro_search` on the Tavily path, so re-read the per-tool shapes. |
| `fallthroughs[]` | Same fall-through policy as `pro_search`. |
| `results[].markdown` / `markdown_chars` / `markdown_truncated_chars` / `truncated` | The whole point of this tool. Truncation is honest: `truncated: true` + the chars tell you how much was cut. |
| `credits_used_this_call` | Firecrawl call cost; useful for after-the-fact budgeting. |
| `usage.remaining_credits` / `plan_credits` / `billing_period_end` / `source` | Live snapshot (`source: "live"`) or last cached probe. |
| `usage.probe_note` | Non-empty when the live probe failed; the snapshot may be stale. |

### `scrape_url`

| Field | Why you read it |
| --- | --- |
| `final_url` | Post-redirect; cite this one, not the input `url`. |
| `markdown` / `markdown_chars` / `markdown_truncated_chars` / `truncated` | The page body in clean markdown. |
| `metadata.title` / `description` / `language` / `url` | Use `metadata.title` as the page title; use `metadata.url` for citing. |
| `credits_used_this_call` | Usually 1; on failure it is not charged. |
| `usage.*` | Same as `deep_search`. |
| `note` | If non-empty + no `markdown`, the scrape failed; do NOT retry the same URL blindly — try `extract_urls` (Tavily's lane) or escalate. |

### `extract_urls`

| Field | Why you read it |
| --- | --- |
| `provider_used` | Always `"tavily"`; there is no parallel batch service. |
| `fallthroughs[]` | Always empty. |
| `results[].raw_content` / `raw_content_truncated_chars` / `truncated` | Per-URL content with the same truncation honesty as `pro_search`. |
| `failed_results[]` | Per-URL error payloads; an HTTP 200 can have entries in BOTH arrays. |
| `usage.calls_this_session` | Tavily counter incremented once per call regardless of how many URLs. |
| `cache` | One entry for the whole batch (the sorted-URL tuple is the cache key). |

### `credit_status`

| Field | Why you read it |
| --- | --- |
| `providers.tavily.calls_this_session` | Tavily counter at this snapshot moment. |
| `providers.firecrawl.remaining_credits` / `plan_credits` / `billing_period_end` | Live probe (or last cached value; check `source`). |
| `providers.firecrawl.probe_note` | Non-empty means the live probe failed; the number may be stale. |
| `providers.duckduckgo.available` | Free tool; only flag is `"available"` vs a transport error. |
| `recommendation.*` | Advisory only; routing never reads this. Use it as a hint, never as authority. |
| `called_at_unix` | Snapshot freshness — pair with `usage.remaining_credits` `source` when the number feels off. |

## Cross-references

- `web-search-budgeting` — for the credit signals behind the routing decisions above.
- `docs/SPECS.md` §3 (tool reference), §6 (chain policy), §8 (budget detail) — the literal source.
- `mcp-fastmcp-2026` — for protocol-level questions (Streamable HTTP, MRTR, tasks).
