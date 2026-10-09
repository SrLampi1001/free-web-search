"""Query / URL normalisation, slug, and FNV-1a 64-bit hash for cache keying.

Adapted from `duck-search-mcp::normalize` for the orchestrator. Same algorithm
(trim, inner-whitespace collapse, lowercase; non-slug chars become `-`; FNV-1a 64
disambiguates queries that slugify identically). The orchestrator stores cache
entries under `<base>/<namespace>/<slug>-<fnv>.json` so each provider's keyspace
is isolated.
"""
from __future__ import annotations

import re
from pathlib import Path

_INNER_WS = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def normalize_query(q: str) -> str:
    if not isinstance(q, str):
        q = str(q)
    return _INNER_WS.sub(" ", q).strip().lower()


def slugify(normalized: str, max_len: int = 80) -> str:
    slug = _NON_SLUG.sub("-", normalized).strip("-")
    if not slug:
        slug = "empty"
    return slug[:max_len]


def fnv1a64(s: str) -> int:
    h = _FNV_OFFSET
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


def cache_path_for(namespace: str, query: str, base_dir: Path) -> Path:
    norm = normalize_query(query)
    return base_dir / namespace / f"{slugify(norm)}-{fnv1a64(norm):016x}.json"


def canonical_batch_key(values: list[str]) -> str:
    """Stable key for an ordered batch like `extract_urls` URLs.

    Order matters per the SPEC §7 cache key tuple shape `("tavily_extract", sorted_tuple_of_urls)`;
    callers may pre-sort if the batch is order-insensitive, otherwise pass the URL list as-is.
    """
    norm = " ".join(normalize_query(v) for v in values)
    return f"batch:{normalize_query(norm)}"
