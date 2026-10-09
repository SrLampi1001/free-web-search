"""Chain-of-Responsibility stub — Phase C sub-agent implements the fall-through
on Auth/Quota/RateLimit only.
"""
from __future__ import annotations


async def try_chain(ordered, **kwargs):  # pragma: no cover - Phase C replaces
    raise NotImplementedError("Phase C will implement try_chain(ordered, **kwargs).")
