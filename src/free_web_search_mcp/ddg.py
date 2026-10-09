"""DuckDuckGo Instant Answer API client and response mapping.

Per SPEC §3.1: the mapper mirrors the legacy `duck-search-mcp::ddg` mapping so
behaviour is unchanged. The Instant Answer API is unauthenticated and free.
- `AbstractText` / `AbstractURL` / `Heading` form the first result.
- `Answer` becomes a result only when no abstract is present.
- `RelatedTopics` is flattened (entries with nested `Topics` recursed) and
  mapped to `{title, url, snippet}` by splitting `Text` on the first " - ".
"""
from __future__ import annotations

import logging
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

DDG_ENDPOINT = "https://api.duckduckgo.com/"
APP_ID = "free-web-search-mcp"

# Hard cap before the user's `count` applies. Keeps `entity_search` from
# materialising megabytes of related topics when a query is unusually broad.
MAPPING_HARD_CAP = 10

EMPTY_NOTE = (
    "DuckDuckGo returned no results — rephrase once as a canonical topic name, "
    "then answer from your own knowledge."
)


async def fetch_ddg(query: str, *, client: httpx.AsyncClient, timeout_s: float) -> dict:
    """Hit the Instant Answer API. Raises on non-2xx; the caller decides what to do."""
    resp = await client.get(
        DDG_ENDPOINT,
        params={
            "q": query,
            "format": "json",
            "t": APP_ID,
            "no_html": "1",
            "skip_disambig": "1",
        },
        timeout=timeout_s,
    )
    resp.raise_for_status()
    return resp.json()


def map_ddg_response(payload: dict) -> list[dict]:
    """Project the upstream JSON onto the `{title, url, snippet}` list the tool returns."""
    results: list[dict] = []
    abstract_text = (payload.get("AbstractText") or "").strip()
    abstract_url = (payload.get("AbstractURL") or "").strip()
    heading = (payload.get("Heading") or "").strip()

    if abstract_text:
        results.append(
            {
                "title": _abstract_title(heading, abstract_text),
                "url": abstract_url,
                "snippet": abstract_text,
            }
        )

    if not results:
        answer = (payload.get("Answer") or "").strip()
        if answer:
            results.append(
                {
                    "title": heading or "Direct answer",
                    "url": "",
                    "snippet": answer,
                }
            )

    for topic in _flatten_related(payload.get("RelatedTopics") or []):
        if len(results) >= MAPPING_HARD_CAP:
            break
        text = (topic.get("Text") or "").strip()
        if not text:
            continue
        if " - " in text:
            title, snippet = text.split(" - ", 1)
        else:
            title, snippet = text[:80], text
        results.append(
            {
                "title": title.strip(),
                "url": (topic.get("FirstURL") or "").strip(),
                "snippet": snippet.strip(),
            }
        )

    return results


def _abstract_title(heading: str, abstract: str) -> str:
    if heading:
        return heading
    first_sentence = abstract.split(".", 1)[0]
    return (first_sentence[:80] + ("…" if len(first_sentence) > 80 else "")).strip()


def _flatten_related(topics: list[dict]) -> Iterable[dict]:
    for t in topics:
        if not isinstance(t, dict):
            continue
        nested = t.get("Topics")
        if isinstance(nested, list):
            yield from _flatten_related(nested)
        elif t.get("Text") or t.get("FirstURL"):
            yield t
