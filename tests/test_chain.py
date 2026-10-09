"""Hermetic tests for the Chain of Responsibility (SPEC §6 fall-through)."""
from __future__ import annotations

import pytest

from free_web_search_mcp.providers.base import (
    ProviderABC,
    ProviderAuthError,
    ProviderEmptyResults,
    ProviderName,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from free_web_search_mcp.providers.chain import ChainExhaustedError, try_chain


# --- FakeProvider: configurable per-test, satisfies ProviderABC structurally ---


class FakeProvider:
    """Adapter stub that raises/returns controllable per-op; no I/O."""

    def __init__(
        self,
        name: ProviderName,
        *,
        returns: dict | None = None,
        raises: BaseException | None = None,
        supports_map: dict[str, bool] | None = None,
    ) -> None:
        self.name = name
        self.configured = True
        self._returns = returns if returns is not None else {}
        self._raises = raises
        self._supports_map = supports_map or {"search": True, "fetch_url": True, "fetch_urls": True}

    def supports(self, operation: str) -> bool:
        return self._supports_map.get(operation, False)

    async def search(self, **_kwargs) -> dict:
        return self._call("search")

    async def fetch_url(self, **_kwargs) -> dict:
        return self._call("fetch_url")

    async def fetch_urls(self, **_kwargs) -> dict:
        return self._call("fetch_urls")

    def _call(self, op: str) -> dict:
        if self._raises is not None:
            raise self._raises
        return self._returns


def _providers(*items: tuple[ProviderName, FakeProvider]) -> dict[ProviderName, ProviderABC]:
    return dict(items)


# --- Tests ------------------------------------------------------------------


async def test_single_provider_succeeds() -> None:
    p = FakeProvider(ProviderName.TAVILY, returns={"results": ["a"]})
    name, response, fallthroughs = await try_chain(
        [ProviderName.TAVILY], _providers((ProviderName.TAVILY, p)), op="search", query="q"
    )
    assert name is ProviderName.TAVILY
    assert response == {"results": ["a"]}
    assert fallthroughs == []


async def test_first_fails_auth_second_succeeds() -> None:
    failing = FakeProvider(
        ProviderName.TAVILY, raises=ProviderAuthError("nope")
    )
    winning = FakeProvider(
        ProviderName.FIRECRAWL, returns={"results": ["x"]}
    )
    providers = _providers((ProviderName.TAVILY, failing), (ProviderName.FIRECRAWL, winning))

    name, response, fallthroughs = await try_chain(
        [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
    )

    assert name is ProviderName.FIRECRAWL
    assert response == {"results": ["x"]}
    assert fallthroughs == [
        {
            "from": "tavily",
            "to": "firecrawl",
            "reason": "ProviderAuthError",
        }
    ]


async def test_quota_error_also_triggers_fallthrough() -> None:
    failing = FakeProvider(ProviderName.TAVILY, raises=ProviderQuotaError("out"))
    winning = FakeProvider(ProviderName.FIRECRAWL, returns={"ok": True})
    providers = _providers((ProviderName.TAVILY, failing), (ProviderName.FIRECRAWL, winning))

    _, _, fallthroughs = await try_chain(
        [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
    )
    assert fallthroughs[0]["reason"] == "ProviderQuotaError"


async def test_ratelimit_error_also_triggers_fallthrough() -> None:
    failing = FakeProvider(ProviderName.FIRECRAWL, raises=ProviderRateLimitError("slow"))
    winning = FakeProvider(ProviderName.TAVILY, returns={"ok": True})
    providers = _providers((ProviderName.FIRECRAWL, failing), (ProviderName.TAVILY, winning))

    _, _, fallthroughs = await try_chain(
        [ProviderName.FIRECRAWL, ProviderName.TAVILY], providers, op="search", query="q"
    )
    assert fallthroughs == [
        {"from": "firecrawl", "to": "tavily", "reason": "ProviderRateLimitError"}
    ]


async def test_all_providers_fail_with_fallthrough_errors_raises_chain_exhausted() -> None:
    a = FakeProvider(ProviderName.TAVILY, raises=ProviderAuthError("a"))
    b = FakeProvider(ProviderName.FIRECRAWL, raises=ProviderRateLimitError("b"))
    providers = _providers((ProviderName.TAVILY, a), (ProviderName.FIRECRAWL, b))

    with pytest.raises(ChainExhaustedError) as ei:
        await try_chain(
            [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
        )
    assert ei.value.fallthroughs == [
        {"from": "tavily", "to": "firecrawl", "reason": "ProviderAuthError"},
        {"from": "firecrawl", "to": None, "reason": "ProviderRateLimitError"},
    ]


async def test_transport_error_does_not_fall_through() -> None:
    half_down = FakeProvider(ProviderName.TAVILY, raises=ProviderUnavailableError("timeout"))
    backup = FakeProvider(ProviderName.FIRECRAWL, returns={"unreachable": True})
    providers = _providers((ProviderName.TAVILY, half_down), (ProviderName.FIRECRAWL, backup))

    with pytest.raises(ProviderUnavailableError):
        await try_chain(
            [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
        )


async def test_empty_results_does_not_fall_through() -> None:
    empty = FakeProvider(ProviderName.TAVILY, raises=ProviderEmptyResults("zero results"))
    backup = FakeProvider(ProviderName.FIRECRAWL, returns={"should_not_see": True})
    providers = _providers((ProviderName.TAVILY, empty), (ProviderName.FIRECRAWL, backup))

    with pytest.raises(ProviderEmptyResults):
        await try_chain(
            [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
        )


async def test_provider_not_supporting_op_is_skipped_silently() -> None:
    """DDG-like provider does not support fetch_url; chain silently moves on."""
    ddg = FakeProvider(
        ProviderName.DUCKDUCKGO, supports_map={"search": True, "fetch_url": False, "fetch_urls": False}
    )
    firecrawl = FakeProvider(ProviderName.FIRECRAWL, returns={"fetched": True})
    providers = _providers((ProviderName.DUCKDUCKGO, ddg), (ProviderName.FIRECRAWL, firecrawl))

    name, response, fallthroughs = await try_chain(
        [ProviderName.DUCKDUCKGO, ProviderName.FIRECRAWL],
        providers,
        op="fetch_url",
        url="https://example.com",
    )

    assert name is ProviderName.FIRECRAWL
    assert response == {"fetched": True}
    assert fallthroughs == []


async def test_provider_not_in_dict_is_skipped_silently() -> None:
    """First name in `ordered` is absent from `providers`; chain moves on."""
    firecrawl = FakeProvider(ProviderName.FIRECRAWL, returns={"ok": True})
    providers = _providers((ProviderName.FIRECRAWL, firecrawl))

    name, response, fallthroughs = await try_chain(
        [ProviderName.TAVILY, ProviderName.FIRECRAWL], providers, op="search", query="q"
    )

    assert name is ProviderName.FIRECRAWL
    assert response == {"ok": True}
    assert fallthroughs == []


async def test_empty_ordered_raises_chain_exhausted_with_empty_fallthroughs() -> None:
    providers = _providers((ProviderName.TAVILY, FakeProvider(ProviderName.TAVILY)))
    with pytest.raises(ChainExhaustedError) as ei:
        await try_chain([], providers, op="search", query="q")
    assert ei.value.fallthroughs == []
