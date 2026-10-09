"""Tests for the shared normalize / cache-key helpers."""
from __future__ import annotations

from free_web_search_mcp.normalize import (
    cache_path_for,
    canonical_batch_key,
    fnv1a64,
    normalize_query,
    slugify,
)


def test_normalize_query_lowers_and_collapses_whitespace() -> None:
    assert normalize_query("  Foo   BAR  ") == "foo bar"


def test_normalize_query_handles_non_strings() -> None:
    assert normalize_query(123) == "123"


def test_slugify_drops_punctuation() -> None:
    assert slugify("c++ vs c#") == "c-vs-c"


def test_slugify_falls_back_to_empty_label() -> None:
    assert slugify("") == "empty"


def test_slugify_caps_length() -> None:
    long = "a" * 200
    assert len(slugify(long, max_len=80)) == 80


def test_fnv1a64_is_stable() -> None:
    assert fnv1a64("rust programming language") == fnv1a64("rust programming language")


def test_fnv1a64_distinguishes_aliases() -> None:
    assert fnv1a64("c++") != fnv1a64("c#")


def test_cache_path_for_namespaces_under_subdir(tmp_path) -> None:
    p_ddg = cache_path_for("duckduckgo", "Rust", tmp_path)
    p_tav = cache_path_for("tavily", "Rust", tmp_path)
    assert p_ddg.parent == tmp_path / "duckduckgo"
    assert p_tav.parent == tmp_path / "tavily"
    assert p_ddg != p_tav  # different parent dirs; same file name is fine


def test_canonical_batch_key_is_stable() -> None:
    assert canonical_batch_key(["https://a", "https://b"]) == canonical_batch_key(["https://a", "https://b"])
    assert canonical_batch_key(["https://a", "https://b"]) != canonical_batch_key(["https://b", "https://a"])
