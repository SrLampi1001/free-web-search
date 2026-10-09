"""Resource implementations per SPEC §4: four read-only views surfaced
via `@mcp.resource`. State is read from `ctx.request_context.lifespan_context`
same as the tool wrappers in `server.py`. Resource bodies live at module level
so tests can call them directly with a stubbed ctx.
"""
from __future__ import annotations

import inspect
import time
from typing import Any

from fastmcp import Context, FastMCP

# SPEC §4.1 — the canonical provider keys; fixed order for stable output.
STATUS_PROVIDERS = ("tavily", "firecrawl", "duckduckgo")
# SPEC §4.4 — SPEC §7 collapses `tavily_extract` + `firecrawl_scrape` into
# their primary namespaces for the operator-telemetry view.
CACHE_PROVIDER_NAMESPACES: dict[str, tuple[str, ...]] = {
    "tavily": ("tavily", "tavily_extract"),
    "firecrawl": ("firecrawl", "firecrawl_scrape"),
    "duckduckgo": ("duckduckgo",),
}


def state_from(ctx: Context | None) -> dict[str, Any]:
    """Pull the lifespan context off the FastMCP request, same shape as
    `server._state_from` so the tool and resource halves stay symmetric."""
    if ctx is None or ctx.request_context is None:
        raise RuntimeError("resource called without a request context")
    span = ctx.request_context.lifespan_context
    if not span:
        raise RuntimeError("server lifespan did not initialise state")
    return span


def _status_row(
    providers: dict[Any, Any],
    name: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Compute one SPEC §4.1 row; falls back to empty defaults when the
    lifespan state does not yet track call/error counters for `name`."""
    keys: set[str] = set()
    for k in providers.keys():
        keys.add(getattr(k, "value", k))
    configured = name in keys
    last_call = float(state.get("last_call_unix", {}).get(name, 0.0) or 0.0)
    last_error = str(state.get("last_error", {}).get(name, "") or "")
    consecutive = int(state.get("consecutive_errors", {}).get(name, 0) or 0)
    return {
        "configured": bool(configured),
        "last_call_unix": last_call,
        "last_error": last_error,
        "consecutive_errors": consecutive,
    }


async def providers_status(ctx: Context | None = None) -> dict:
    """SPEC §4.1 per-provider reachability / errors snapshot."""
    state = state_from(ctx)
    providers = state["providers"]
    return {name: _status_row(providers, name, state) for name in STATUS_PROVIDERS}


async def providers_budget(ctx: Context | None = None) -> dict:
    """SPEC §4.2 merged budget snapshot + `called_at_unix` stamp."""
    state = state_from(ctx)
    snap: dict[str, Any] = dict(state["budget"].snapshot())
    snap["called_at_unix"] = time.time()
    return snap


async def providers_schema(ctx: Context | None = None, *, mcp: FastMCP | None = None) -> dict:
    """SPEC §4.3 introspect the live FastMCP instance; pass `mcp=` when
    calling outside a real request (tests)."""
    if mcp is None:
        raise RuntimeError(
            "providers_schema requires the FastMCP instance; "
            "pass it explicitly via mcp= when calling outside a real request."
        )
    tools_seq = await mcp.list_tools()
    resources_seq = await mcp.list_resources()
    templates_seq = await mcp.list_resource_templates()
    prompts_seq = await mcp.list_prompts()

    tools_out: list[dict[str, Any]] = []
    for t in tools_seq:
        tools_out.append({
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.parameters,
            "output_schema": t.output_schema,
        })

    resources_out: list[dict[str, Any]] = []
    for r in resources_seq:
        resources_out.append({
            "uri": str(r.uri),
            "name": r.name,
            "content_type": r.mime_type or "text/plain",
            "description": r.description or "",
        })
    for rt in templates_seq:
        uri_str = (
            str(rt.uri_template)
            if hasattr(rt, "uri_template")
            else str(getattr(rt, "uri", ""))
        )
        resources_out.append({
            "uri": uri_str,
            "name": rt.name,
            "content_type": rt.mime_type or "text/plain",
            "description": rt.description or "",
        })

    prompts_out: list[dict[str, Any]] = []
    for p in prompts_seq:
        arg_types: dict[str, Any] = {}
        try:
            sig = inspect.signature(p.fn)
            for pname, pinfo in sig.parameters.items():
                ann = pinfo.annotation
                arg_types[pname] = ann if ann is not inspect.Parameter.empty else str
        except (TypeError, ValueError):
            arg_types = {}
        args: list[dict[str, Any]] = []
        for a in (p.arguments or []):
            ann = arg_types.get(a.name, str)
            type_name = getattr(ann, "__name__", str(ann))
            args.append({"name": a.name, "type": type_name, "required": bool(a.required)})
        prompts_out.append({
            "name": p.name,
            "description": p.description or "",
            "arguments": args,
            "body": p.description or "",
        })

    return {"tools": tools_out, "resources": resources_out, "prompts": prompts_out}


async def cache_stats(ctx: Context | None = None) -> dict:
    """SPEC §4.4 per-provider cache hit/miss counters + aggregated totals."""
    state = state_from(ctx)
    cache = state["cache"]
    per: dict[str, Any] = {}
    totals_hits = 0
    totals_misses = 0
    totals_bytes = 0
    for name, namespaces in CACHE_PROVIDER_NAMESPACES.items():
        hits = sum(cache.hit_count(ns) for ns in namespaces)
        misses = sum(cache.miss_count(ns) for ns in namespaces)
        size = sum(cache.dir_size_bytes(ns) for ns in namespaces)
        per[name] = {
            "hits": int(hits),
            "misses": int(misses),
            "dir_size_bytes": int(size),
        }
        totals_hits += int(hits)
        totals_misses += int(misses)
        totals_bytes += int(size)
    return {
        **per,
        "totals": {
            "hits": totals_hits,
            "misses": totals_misses,
            "dir_size_bytes": totals_bytes,
            "last_purge_unix": 0,
        },
    }


def register_resources(mcp: FastMCP) -> None:
    """Bind the four SPEC §4 resources to `mcp`. Pure registration, no side effects."""

    @mcp.resource(
        uri="providers://status",
        name="providers_status",
        mime_type="application/json",
        annotations={"readOnlyHint": True, "idempotentHint": True},
        description=(
            "SPEC §4.1 per-provider reachability: configured flag, last call "
            "timestamp, last error string, and consecutive-error counter."
        ),
    )
    async def _status_resource(ctx: Context | None = None) -> dict:
        return await providers_status(ctx)

    @mcp.resource(
        uri="providers://budget",
        name="providers_budget",
        mime_type="application/json",
        annotations={"readOnlyHint": True, "idempotentHint": True},
        description=(
            "SPEC §4.2 merged budget snapshot across the three providers, "
            "plus the routing recommendation block and a `called_at_unix` stamp."
        ),
    )
    async def _budget_resource(ctx: Context | None = None) -> dict:
        return await providers_budget(ctx)

    @mcp.resource(
        uri="providers://schema",
        name="providers_schema",
        mime_type="application/json",
        annotations={"readOnlyHint": True, "idempotentHint": True},
        description=(
            "SPEC §4.3 combined JSON-Schema for every tool, resource, and "
            "prompt registered on this server."
        ),
    )
    async def _schema_resource(ctx: Context | None = None) -> dict:
        # `mcp` here is the enclosing FastMCP — captured from `register_resources`.
        return await providers_schema(ctx, mcp=mcp)

    @mcp.resource(
        uri="cache://stats",
        name="cache_stats",
        mime_type="application/json",
        annotations={"readOnlyHint": True, "idempotentHint": True},
        description=(
            "SPEC §4.4 per-provider cache hit/miss counters and disk size, "
            "plus an aggregated `totals` block."
        ),
    )
    async def _cache_resource(ctx: Context | None = None) -> dict:
        return await cache_stats(ctx)


__all__ = [
    "register_resources",
    "providers_status",
    "providers_budget",
    "providers_schema",
    "cache_stats",
    "state_from",
    "STATUS_PROVIDERS",
    "CACHE_PROVIDER_NAMESPACES",
]
