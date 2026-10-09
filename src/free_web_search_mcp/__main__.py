"""Entry point: `python -m free_web_search_mcp` and the `free-web-search-mcp` script.

Best-effort `.env` loader mirrors the `fireclaw-search-mcp::__main__` pattern:
python-dotenv is installed transitively via FastMCP 4; if it ever disappears,
the orchestrator just loses `.env` auto-loading. Real env vars always win.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .server import mcp
from .settings import Settings, configure_logging

log = logging.getLogger(__name__)

_DOTENV_CANDIDATES = (".env",)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for name in _DOTENV_CANDIDATES:
        path = Path(name)
        if path.is_file():
            load_dotenv(path, override=False)
            return


def main() -> None:
    _load_dotenv()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if settings.transport == "stdio":
        log.info("starting stdio transport (tavily=%s firecrawl=%s ddg=%s)",
                 settings.tavily_enabled, settings.firecrawl_enabled, settings.ddg_enabled)
        mcp.run(transport="stdio")
        return
    log.info(
        "starting Streamable HTTP on http://%s:%d%s (tavily=%s firecrawl=%s ddg=%s)",
        settings.host,
        settings.port,
        settings.path,
        settings.tavily_enabled,
        settings.firecrawl_enabled,
        settings.ddg_enabled,
    )
    mcp.run(transport="http", host=settings.host, port=settings.port, path=settings.path)


if __name__ == "__main__":
    main()
