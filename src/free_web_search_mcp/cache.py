"""Namespaced on-disk TTL cache shared by all providers.

Per SPEC §7: `DiskCache` takes a `(namespace, query)` pair so each provider's
keyspace is isolated. File name = `<slug>-<fnv1a64>.json`; the namespace is a
subdirectory under the cache root. A missing, stale, corrupt, or unwritable
entry never fails the call; the cache layer logs a `warn` and degrades to a
plain uncached call.

This is a refactor of `duck-search-mcp::cache::DiskCache` for the
multi-provider orchestrator: same semantics, namespaced key.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .normalize import cache_path_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedResponse:
    namespace: str
    query: str
    kind: str
    response: dict


class DiskCache:
    """Filesystem TTL cache. `ttl_seconds <= 0` disables the cache entirely."""

    def __init__(self, base_dir: Path, ttl_seconds: int):
        self._dir = Path(base_dir)
        self._ttl = max(0, int(ttl_seconds))

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    @property
    def base_dir(self) -> Path:
        return self._dir

    def get(self, namespace: str, query: str) -> CachedResponse | None:
        if not self.enabled:
            return None
        path = self._path_for(namespace, query)
        try:
            if not path.exists():
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = float(envelope.get("fetched_at_unix", 0))
            if (time.time() - fetched_at) > self._ttl:
                return None
            inner = envelope.get("response")
            if not isinstance(inner, dict):
                log.warning("cache: %s has malformed response; ignoring", path)
                return None
            return CachedResponse(
                namespace=namespace,
                query=query,
                kind=envelope.get("kind", namespace),
                response=inner,
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("cache: read failed for %r ns=%s (%s); falling through", query, namespace, e)
            return None

    def set(self, namespace: str, query: str, kind: str, response: dict) -> None:
        if not self.enabled:
            return
        path = self._path_for(namespace, query)
        envelope = {
            "fetched_at_unix": time.time(),
            "namespace": namespace,
            "query": query,
            "kind": kind,
            "response": response,
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / namespace).mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.warning("cache: write failed for %r ns=%s (%s); continuing without cache", query, namespace, e)

    def invalidate(self, namespace: str, query: str) -> None:
        if not self.enabled:
            return
        try:
            self._path_for(namespace, query).unlink(missing_ok=True)
        except OSError as e:
            log.warning("cache: invalidate failed for %r ns=%s", query, namespace, e)

    def dir_size_bytes(self, namespace: str | None = None) -> int:
        """Total bytes of cache files under `namespace` (or all if `None`)."""
        root = self._dir / namespace if namespace else self._dir
        if not root.exists():
            return 0
        total = 0
        for p in root.rglob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    def hit_count(self, namespace: str) -> int:
        return self._stats(namespace).hits

    def miss_count(self, namespace: str) -> int:
        return self._stats(namespace).misses

    def _stats(self, namespace: str) -> "_Stats":
        if not hasattr(self, "_stats_counters"):
            self._stats_counters = {}
        if namespace not in self._stats_counters:
            self._stats_counters[namespace] = _Stats()
        return self._stats_counters[namespace]

    def _path_for(self, namespace: str, query: str) -> Path:
        return cache_path_for(namespace, query, self._dir)


@dataclass
class _Stats:
    hits: int = 0
    misses: int = 0
