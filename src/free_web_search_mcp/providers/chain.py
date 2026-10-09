"""Chain of Responsibility — SPEC §6 fall-through policy.

Runs `op` across the providers the router picked, falling through on
`ProviderAuthError` / `ProviderQuotaError` / `ProviderRateLimitError` only.
Transport errors, generic `ProviderError`, and adapter `EmptyResults` surface
immediately (no runaway retry on a half-down provider).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .base import (
    ProviderABC,
    ProviderAuthError,
    ProviderError,
    ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
)


class ChainExhaustedError(ProviderError):
    """All candidates in the ordered chain failed or were skipped."""

    def __init__(self, fallthroughs: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            f"All providers in the chain failed or were skipped; fallthroughs={fallthroughs}"
        )
        self.fallthroughs = list(fallthroughs or [])


# Exceptions that trigger fall-through to the next provider.
_FALLTHROUGH_EXC: tuple[type[BaseException], ...] = (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRateLimitError,
)


async def try_chain(
    ordered: Sequence[ProviderName],
    providers: dict[ProviderName, ProviderABC],
    op: str,
    **kwargs: Any,
) -> tuple[ProviderName, dict[str, Any], list[dict[str, Any]]]:
    """Run `provider.<op>(**kwargs)` across the ordered providers.

    Returns `(winning_name, response_dict, fallthroughs)`. Each fallthrough
    element is `{"from": "<name>", "to": "<next_name>|None", "reason": "<ExcName>"}`.

    Skips silently (no fallthrough recorded) when a name is missing from
    `providers` or when its `supports(op)` is False.
    """
    fallthroughs: list[dict[str, Any]] = []

    for idx, name in enumerate(ordered):
        adapter = providers.get(name)
        if adapter is None:
            # Not configured; never tried, not a fall-through.
            continue
        if not adapter.supports(op):
            # Wrong shape; never tried, not a fall-through.
            continue
        method = getattr(adapter, op, None)
        if method is None or not callable(method):
            continue

        next_name = ordered[idx + 1] if idx + 1 < len(ordered) else None

        try:
            response = await method(**kwargs)
        except _FALLTHROUGH_EXC as exc:
            fallthroughs.append(
                {
                    "from": name.value,
                    "to": next_name.value if next_name is not None else None,
                    "reason": exc.__class__.__name__,
                }
            )
            continue
        # Any other exception (Unavailable, ProviderError, EmptyResults, …)
        # surfaces immediately per SPEC §6 / §14.
        return (name, response, fallthroughs)

    raise ChainExhaustedError(fallthroughs)


__all__ = ["ChainExhaustedError", "try_chain"]
