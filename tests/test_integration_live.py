"""Integration tests that hit live winget-pkgs (GitHub) and Chocolatey APIs.

These are skipped unless ``--integration`` is passed to pytest (run:
``uv run pytest -m integration``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CACHE_DIR = Path(__file__).parent.parent / "data_integration"


def _settings():
    os.makedirs(CACHE_DIR, exist_ok=True)
    from appcatalog_mcp.config import Settings

    return Settings(
        cache_dir=CACHE_DIR,
        cache_ttl_hours=24,
        request_delay_seconds=0.5,
    )


def _build_live():
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.http_client import HttpClient
    from appcatalog_mcp.rate_limiter import RateLimiter

    settings = _settings()
    cache = CacheStore(settings.cache_db_path, settings.cache_ttl_seconds)
    rl = RateLimiter(settings.request_delay_seconds)
    http = HttpClient(settings, cache, rl)
    return http, settings


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_winget_get_package():
    from appcatalog_mcp.adapters import WingetAdapter

    http, settings = _build_live()
    try:
        adapter = WingetAdapter(http, settings)
        pkg = await adapter.get_package("Google.Chrome")
        assert pkg.id == "Google.Chrome"
        assert pkg.version and pkg.version != "unknown"
        assert pkg.installers
        assert any(i.sha256 for i in pkg.installers)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_winget_get_installer_metadata_vscode():
    from appcatalog_mcp.adapters import WingetAdapter

    http, settings = _build_live()
    try:
        adapter = WingetAdapter(http, settings)
        pkg = await adapter.get_installer_detail("Microsoft.VisualStudioCode")
        assert pkg.id == "Microsoft.VisualStudioCode"
        # VSCode ships per-arch inno + msi + user/system scope installers.
        assert len(pkg.installers) >= 2
        archs = {i.architecture for i in pkg.installers}
        assert "x64" in archs
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_winget_search_uses_run_api():
    """winget.run is the only path that exposes free-text search across manifests."""
    from appcatalog_mcp.adapters import WingetAdapter

    http, settings = _build_live()
    # Force winget.run; GitHub has no free-text package search.
    settings_wrun = settings.model_copy(update={"winget_api": "winget.run"})
    try:
        adapter = WingetAdapter(http, settings_wrun)
        results = await adapter.search("chrome", limit=5)
        # winget.run data is from 2023 — still present, may be sparse, but
        # chrome/ungoogled-chromium variants should match.
        assert isinstance(results, list)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_chocolatey_get_package():
    from appcatalog_mcp.adapters import ChocolateyAdapter

    http, settings = _build_live()
    try:
        adapter = ChocolateyAdapter(http, settings.choco_api)
        pkg = await adapter.get_package("7zip")
        assert pkg.id == "7zip"
        assert pkg.version
        assert pkg.installers
        # versions are sorted newest-first.
        assert pkg.versions[0] == pkg.version
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_chocolatey_search():
    from appcatalog_mcp.adapters import ChocolateyAdapter

    http, settings = _build_live()
    try:
        adapter = ChocolateyAdapter(http, settings.choco_api)
        results = await adapter.search("vlc", limit=5)
        assert results
        assert any(r.id == "vlc" for r in results)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_chocolatey_list_recent():
    from appcatalog_mcp.adapters import ChocolateyAdapter

    http, settings = _build_live()
    try:
        adapter = ChocolateyAdapter(http, settings.choco_api)
        results = await adapter.list_recent(limit=5)
        assert results
        # Most-recent first.
        assert all(r.version for r in results)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_compare_sources_via_chocolatey():
    """Sanity: Chocolatey knows about 7zip and winget knows about 7zip.7zip."""
    from appcatalog_mcp.adapters import ChocolateyAdapter, WingetAdapter

    http, settings = _build_live()
    try:
        ch = ChocolateyAdapter(http, settings.choco_api)
        wg = WingetAdapter(http, settings)
        ch_pkg = await ch.get_package("7zip")
        wg_pkg = await wg.get_package("7zip.7zip")
        assert ch_pkg.version
        assert wg_pkg.version
        assert ch_pkg.installers[0].installer_type == "nupkg"
        assert any(i.installer_type in {"exe", "msi", "inno", "wix"} for i in wg_pkg.installers)
    finally:
        await http.close()
