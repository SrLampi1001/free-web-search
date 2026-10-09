---
name: web-search-budgeting
description: > Budget reading and session planning for `free-web-search-mcp` v0.1 — reads the merged `credit_status()` response and the `providers://budget` resource snapshot, encodes the per-provider budget shape (Tavily in-process `calls_this_session`, Firecrawl live `/v2/team/credit-usage` probe, DDG free with no budget signal), and applies the §8 switch-to-free heuristics (Firecrawl below 50, Tavily above 200 calls, both tight). Use at the start of a session, before any expensive batch of paid calls, and after every batch to re-evaluate. The `recommendation` block is advisory only — never trust it over what the orchestrator actually did (read `provider_used` + `fallthroughs[]` in the tool response). Cross-reference `web-search-routing` for tool selection and SPEC §8 for the source-of-truth table.
version: 0.1.0
tags:
  - free-web-search-mcp
  - budgeting
  - credit-status
  - tavily
  - firecrawl
  - duckduckgo
---

# Web search budgeting — `free-web-search-mcp` v0.1

## When to use this skill

Read this skill before the first paid call of a session and again after every batch of calls. Two specific triggers: (a) you are about to call `pro_search`, `deep_search`, `scrape_url`, or `extract_urls` more than once in a row, or (b) a prior `pro_search` / `deep_search` response reported a thin `remaining_credits` number. Tool selection lives in `web-search-routing`; that skill sits downstream of the budget decision this one drives.

## The three providers at a glance

| Provider | Budget signal type | Refresh rate | Where to read it |
| --- | --- | --- | --- |
| `tavily` | In-process `CallCounter` (`calls_this_session`) | Per-call; local-only | `providers.tavily.calls_this_session` in `credit_status()` and `providers://budget`; `usage.calls_this_session` in `pro_search` / `extract_urls` responses. |
| `firecrawl` | Live `/v2/team/credit-usage` probe (`remaining_credits`, `plan_credits`, `billing_period_end`) | At `credit_status()` time + cached value with `source: "cached"` | `providers.firecrawl.*` in the same places; per-call `usage.*` snapshot in `deep_search` / `scrape_url` responses. |
| `duckduckgo` | None (free, no key, no auth, no rate limit beyond `FWS_DDG_MIN_INTERVAL_MS`) | N/A | `providers.duckduckgo.available: true` in `credit_status()`; informational only. |

Tavily's `note` literally states there is no remote credit endpoint — the counter is local, so two orchestrator processes with the same `TAVILY_API_KEY` drift independently. Firecrawl exposes a live probe AND charges credits locally (`CreditTracker.charge(n)` after each successful call); the two should agree to within one call's worth, and when they don't the `probe_note` field is the diagnostic. DDG is always green.

## How to read `credit_status()` and `providers://budget`

`providers://budget` is the resource-side mirror of `credit_status()` (same shape, same contents, same `called_at_unix`). Read either one — they're interchangeable for budgeting purposes. The resource has `ttlMs ≈ 60 s` and `cacheScope: "private"`, so freshness is appropriate for per-session budget decisions. The fields the agent actually consumes:

| Field | Provider | What it tells you |
| --- | --- | --- |
| `providers.tavily.calls_this_session` | Tavily | How many Tavily calls this process has made so far. |
| `providers.tavily.note` | Tavily | Always present; reminds you the counter is local. |
| `providers.firecrawl.remaining_credits` | Firecrawl | Credits left in the current billing period. |
| `providers.firecrawl.plan_credits` | Firecrawl | Plan-tier credit limit. Reset at `billing_period_end`. |
| `providers.firecrawl.billing_period_end` | Firecrawl | ISO-8601 instant; plan resets there (don't conflate with server-local time). |
| `providers.firecrawl.source` | Firecrawl | `"live"` (fresh probe) vs `"cached"` (probe failed last time). |
| `providers.firecrawl.probe_note` | Firecrawl | Non-empty when the live probe failed — number may be stale. |
| `providers.duckduckgo.available` | DDG | Free tool liveness; only meaningful as a sanity check. |
| `recommendation.entity_queries` … `batch_urls` | merged | Advisory routing hints — see `Switch-to-free heuristics` below. |
| `recommendation.switch_to_free_when` | merged | Two simple thresholds; use them as a shortcut, not the full policy. |
| `called_at_unix` | meta | Snapshot freshness; pair with the `source` and `probe_note` fields when the number feels off. |

The `recommendation` block encodes the §8 table at a glance. **It is advisory only.** The orchestrator's routing is fixed by SPEC §6; `credit_status()` does not change which provider actually runs. The agent must always read the **actual** `provider_used` and `fallthroughs[]` in the tool response — never trust `recommendation.deep_retrieval: "entity_search"` over `provider_used: "firecrawl"`. The recommendation is a hint about what *would* have been cheaper; the tool response is what *happened*.

## Switch-to-free heuristics

These are the threshold rules from SPEC §8, expressed as a decision table. Always pair them with the tool response: the heuristic describes the next call you should make, not the call you just made.

| Condition | `entity_queries` | `general_questions` | `deep_retrieval` | `single_url` | `batch_urls` |
| --- | --- | --- | --- | --- | --- |
| Both paid providers healthy (Firecrawl ≥ 50 AND Tavily < 200) | `entity_search` | `pro_search` | `deep_search` | `scrape_url` | `extract_urls` |
| Firecrawl < 50 AND Tavily < 200 | `entity_search` | `pro_search` | `pro_search` (markdown via `raw_content`) | `scrape_url` (still works, costs credits) | `extract_urls` |
| Firecrawl < 50 AND Tavily ≥ 200 | `entity_search` | `entity_search` | `entity_search` | `scrape_url` (still works) | `extract_urls` (still works) |
| Tavily not configured AND Firecrawl out of credits | `entity_search` | `entity_search` | `entity_search` | `scrape_url` (`ToolError`) | `extract_urls` (`ToolError`) |

Three plain-English thresholds to remember:

- **`firecrawl.remaining_credits < 50`** → prefer `entity_search` and `pro_search` for follow-ups; `deep_search` and `scrape_url` still work but cost credits. The 50-credit guard (`FWS_FIRECRAWL_MIN_CREDITS`) raises `ToolError` on calls below it; that error is real, not a fall-through signal.
- **`tavily.calls_this_session >= 200`** → prefer `entity_search` (DDG) for everything; both paid tools may fail on the orchestrator's `FWS_TAVILY_MAX_CALLS` advisory guard. The Tavily guard is advisory, not enforced, but the orchestrator will surface a `ToolError` once exceeded.
- **Both tight** → `entity_search` is the only tool that always works; everything else carries budget risk. Plan the rest of the session around DDG snippets and the model's prior knowledge.

When the orchestrator's recommendation table disagrees with these thresholds, prefer the table. When both tables disagree with the tool response, prefer the tool response (`provider_used` and the `usage.*` block are read-from-the-network truth).

## Session-level budget pattern

The pattern below is the recommended loop. It costs one `credit_status` call at the start (free — DDG is the only mandatory reading; Tavily reads from in-process; Firecrawl's probe does not charge credits). After that, each paid tool call returns its own `usage.*` block, which is the authoritative per-call snapshot.

```
At session start:
  1. Call credit_status (or read providers://budget).
  2. Read providers.tavily.calls_this_session, providers.firecrawl.remaining_credits,
     providers.firecrawl.source, and the recommendation block.
  3. Pick the mode for the session:
       free-only      — Firecrawl < 50 AND Tavily >= 200
       paid-mixed     — both healthy (default)
       deep-only      — Firecrawl healthy AND topic is complex enough to need markdown
  4. Plan the next ~5 calls inside that mode. If unsure, default to paid-mixed.

Before an expensive batch (e.g. 3+ paid calls in a row):
  5. Re-call credit_status. Re-read the same four fields.
  6. If the mode has shifted, replan.

After every paid tool call:
  7. Read usage.* in the response. Update your mental model.
       - Tavily: usage.calls_this_session just incremented.
       - Firecrawl: usage.remaining_credits dropped by credits_used_this_call.
  8. If the response is cache: "hit", usage is still captured at response time
     but the upstream was NOT charged; the credit signal is unchanged.
  9. If usage.source == "cached" on Firecrawl, the live probe failed last time;
     your number may be off by one call. Re-call credit_status to refresh.

After the batch:
  10. Re-call credit_status. Compare to the pre-batch snapshot.
       - Tavily delta = number of pro_search / extract_urls calls that hit the network.
       - Firecrawl delta = sum of credits_used_this_call across deep_search / scrape_url calls.
  11. If a threshold was crossed, re-pick the mode for the rest of the session.
  12. If both providers are now flat (Firecrawl < 50 AND Tavily >= 200),
      collapse to entity_search-only and stop calling paid tools speculatively.
```

A worked mini-example:

```text
Turn 1:   credit_status                   → Tavily=3, Firecrawl=812/1000, source=live
Turn 2:   entity_search("Rust language")  → source: upstream, usage not applicable
Turn 3:   pro_search("tokio json logger") → provider_used: "tavily",
                                            usage.calls_this_session: 4
Turn 4:   credit_status                   → Tavily=4, Firecrawl=809/1000 (paid 3 credits)
Turn 5:   deep_search("…more context…")   → usage.remaining_credits: 802,
                                            credits_used_this_call: 7
Turn 6:   credit_status                   → Tavily=5, Firecrawl=802/1000
                                          → still paid-mixed; keep planning with paid tools
```

Note `credit_status` does NOT burn credits itself; calling it is always safe. Use it liberally between paid batches.

## Pitfalls

- **A `cache: "hit"` response does not mean zero spend.** Cache hits do NOT decrement the Tavily counter and do NOT charge Firecrawl; but they DO indicate the orchestrator has fresh data. Don't confuse "cheap to retrieve" with "free to learn from".
- **`usage.*` from a cache-hit response is captured at response time.** It reflects the orchestrator's last successful upstream call for that cache entry, not the current moment. If you need the live number, re-call `credit_status`.
- **Don't trust the `recommendation` block over `provider_used`.** The recommendation may say "deep_retrieval: entity_search" because the budget is tight, but the orchestrator still calls `deep_search` when you call it. The recommendation does not auto-rewrite your calls.
- **Don't read `firecrawl.remaining_credits` alone.** Pair it with `plan_credits` to know the percentage left, and with `billing_period_end` to know whether waiting until next period is cheaper than spending now.
- **Don't ignore `probe_note`.** Non-empty means the live probe failed; `source` may be `"cached"` and the number could be off by a few credits. Re-call `credit_status` and watch for `source: "live"` before treating the number as authoritative.
- **Tavily's counter is process-local.** Two orchestrator processes with the same `TAVILY_API_KEY` each have their own counter. Within a single process, it is authoritative.
- **DDG is always green, but not always useful.** Cheap per-call, but entity-shaped only; complex topics return empty and you're back to your own knowledge. Don't substitute DDG for a real search if the topic demands citation from the web.
- **The `FWS_TAVILY_MAX_CALLS` guard is advisory.** Once exceeded, `pro_search` / `extract_urls` raise `ToolError`; the counter does not auto-reset. The fix is to wait for billing reset, switch providers, or restart the orchestrator.
- **The `FWS_FIRECRAWL_MIN_CREDITS` guard is enforced.** Below 50 credits, `deep_search` raises `ToolError` BEFORE calling the upstream. That `ToolError` is intentional budget protection, not a fall-through signal; re-route, don't retry.

## Cross-references

- `web-search-routing` — for which tool to pick given a budget posture.
- `docs/SPECS.md` §6 (chain policy), §8 (the source-of-truth recommendation table).
- `mcp-fastmcp-2026` — for protocol-level questions (Streamable HTTP, MRTR, tasks).
