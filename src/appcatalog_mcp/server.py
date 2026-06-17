"""FastMCP app wiring + lifespan management for the app catalog server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from appcatalog_mcp.adapters import (
    ChocolateyAdapter,
    EvergreenAdapter,
    SihqAdapter,
    WingetAdapter,
)
from appcatalog_mcp.cache import CacheStore
from appcatalog_mcp.config import Settings, get_settings
from appcatalog_mcp.http_client import HttpClient
from appcatalog_mcp.rate_limiter import RateLimiter
from appcatalog_mcp.tools import register_tools

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    settings: Settings = server.app_settings  # type: ignore[attr-defined]
    cache = CacheStore(settings.cache_db_path, settings.cache_ttl_seconds)
    rate_limiter = RateLimiter(settings.request_delay_seconds)
    http = HttpClient(settings, cache, rate_limiter)

    winget = WingetAdapter(http, settings)
    choco = ChocolateyAdapter(http, settings.choco_api)
    evergreen = EvergreenAdapter(http, settings.evergreen_api)
    sihq = SihqAdapter(http, settings.sihq_url)

    try:
        yield {
            "settings": settings,
            "http": http,
            "cache": cache,
            "winget": winget,
            "chocolatey": choco,
            "evergreen": evergreen,
            "sihq": sihq,
        }
    finally:
        await http.close()
        logger.info("HTTP client closed")


def create_mcp(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    mcp = FastMCP(
        "appcatalog",
        instructions=(
            "Aggregate application metadata across winget, Chocolatey, and Silent "
            "Install HQ: latest versions, download URLs, SHA256 hashes, installer "
            "types/architectures/scope, product and upgrade codes, silent install "
            "switches, dependencies, and release notes. Cross-platform — does not "
            "require winget to be installed locally."
        ),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
        json_response=True,
        stateless_http=True,
        lifespan=app_lifespan,
    )
    mcp.app_settings = settings  # type: ignore[attr-defined]

    register_tools(mcp)
    return mcp
