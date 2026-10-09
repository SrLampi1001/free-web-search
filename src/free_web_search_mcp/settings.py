"""Environment-driven configuration for the orchestrator.

Per SPEC §9: env vars are namespaced `FWS_*` for orchestrator-level settings
and `FWS_<PROVIDER>_*` for per-provider overrides. The API-key env vars are
NOT namespaced — `TAVILY_API_KEY` and `FIRECRAWL_API_KEY` are accepted as-is
so users can copy sibling keys verbatim.

All settings are read at process start; nothing here mutates global state.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a float; got {raw!r}") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_log_level() -> int:
    raw = _env_str(
        "FWS_LOG_LEVEL",
        _env_str("MCP_LOG_LEVEL", _env_str("FASTMCP_LOG_LEVEL", "WARNING")),
    ).upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if raw not in mapping:
        raise ValueError(f"FWS_LOG_LEVEL must be one of {sorted(mapping)}; got {raw!r}")
    return mapping[raw]


def _resolve_tavily_key() -> str:
    key = os.environ.get("TAVILY_API_KEY")
    return key.strip() if key else ""


def _resolve_firecrawl_key() -> str:
    key = (
        os.environ.get("FIRECRAWL_API_KEY")
        or os.environ.get("FIRECLAW_API_KEY")
    )
    return key.strip() if key else ""


@dataclass(frozen=True)
class Settings:
    transport: str
    host: str
    port: int
    path: str
    cache_dir: Path
    http_timeout_s: float
    log_level: int

    tavily_api_key: str
    firecrawl_api_key: str

    ddg_enabled: bool
    ddg_cache_ttl_secs: int
    ddg_min_interval_ms: int

    tavily_enabled: bool
    tavily_base_url: str
    tavily_cache_ttl_secs: int
    tavily_min_interval_ms: int
    tavily_max_calls: int
    tavily_max_raw_chars: int

    firecrawl_enabled: bool
    firecrawl_base_url: str
    firecrawl_cache_ttl_secs: int
    firecrawl_min_interval_ms: int
    firecrawl_min_credits: int
    firecrawl_max_markdown_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        transport = _env_str("FWS_TRANSPORT", "http").lower()
        if transport not in {"http", "stdio"}:
            raise ValueError(f"FWS_TRANSPORT must be 'http' or 'stdio'; got {transport!r}")

        port = _env_int("FWS_PORT", 8000)
        if not (1 <= port <= 65535):
            raise ValueError(f"FWS_PORT must be in [1, 65535]; got {port}")

        tavily_key = _resolve_tavily_key()
        firecrawl_key = _resolve_firecrawl_key()
        tavily_enabled = _env_bool("FWS_TAVILY_ENABLED", bool(tavily_key))
        firecrawl_enabled = _env_bool("FWS_FIRECRAWL_ENABLED", bool(firecrawl_key))

        min_credits = _env_int("FWS_FIRECRAWL_MIN_CREDITS", 50)
        if min_credits < 0:
            raise ValueError("FWS_FIRECRAWL_MIN_CREDITS must be >= 0")

        max_md = _env_int("FWS_FIRECRAWL_MAX_MARKDOWN_CHARS", 8000)
        if max_md < 256:
            raise ValueError("FWS_FIRECRAWL_MAX_MARKDOWN_CHARS must be >= 256")

        tavily_max = _env_int("FWS_TAVILY_MAX_CALLS", 200)
        if tavily_max < 0:
            raise ValueError("FWS_TAVILY_MAX_CALLS must be >= 0")

        return cls(
            transport=transport,
            host=_env_str("FWS_HOST", "127.0.0.1"),
            port=port,
            path=_env_str("FWS_PATH", "/mcp"),
            cache_dir=Path(_env_str("FWS_CACHE_DIR", "./cache/fws")).expanduser().resolve(),
            http_timeout_s=_env_float("FWS_HTTP_TIMEOUT_S", 30.0),
            log_level=_resolve_log_level(),
            tavily_api_key=tavily_key,
            firecrawl_api_key=firecrawl_key,
            ddg_enabled=_env_bool("FWS_DDG_ENABLED", True),
            ddg_cache_ttl_secs=_env_int("FWS_DDG_CACHE_TTL_SECS", 86400),
            ddg_min_interval_ms=_env_int("FWS_DDG_MIN_INTERVAL_MS", 1100),
            tavily_enabled=tavily_enabled,
            tavily_base_url=_env_str("FWS_TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/"),
            tavily_cache_ttl_secs=_env_int("FWS_TAVILY_CACHE_TTL_SECS", 86400),
            tavily_min_interval_ms=_env_int("FWS_TAVILY_MIN_INTERVAL_MS", 200),
            tavily_max_calls=tavily_max,
            tavily_max_raw_chars=_env_int("FWS_TAVILY_MAX_RAW_CHARS", 8000),
            firecrawl_enabled=firecrawl_enabled,
            firecrawl_base_url=_env_str(
                "FWS_FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v2"
            ).rstrip("/"),
            firecrawl_cache_ttl_secs=_env_int("FWS_FIRECRAWL_CACHE_TTL_SECS", 86400),
            firecrawl_min_interval_ms=_env_int("FWS_FIRECRAWL_MIN_INTERVAL_MS", 0),
            firecrawl_min_credits=min_credits,
            firecrawl_max_markdown_chars=max_md,
        )


def configure_logging(level: int) -> None:
    """Logs go to stderr; stdout is the JSON-RPC stream."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
