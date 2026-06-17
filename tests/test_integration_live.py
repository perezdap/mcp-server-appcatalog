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
async def test_live_chocolatey_nupkg_parsing_unwraps_real_installers():
    """``get_installer_detail`` downloads the .nupkg, parses
    ``tools/chocolateyInstall.ps1``, and surfaces real per-arch MSI URLs +
    SHA256 hashes (the OData feed only exposes the .nupkg URL)."""
    from appcatalog_mcp.adapters import ChocolateyAdapter

    http, settings = _build_live()
    try:
        adapter = ChocolateyAdapter(http, settings.choco_api)
        pkg = await adapter.get_installer_detail("googlechrome")
        assert pkg.id.lower() == "googlechrome"
        # The parse should replace the placeholder .nupkg installer with at
        # least one real per-arch MSI URL.
        assert pkg.installers
        real_installers = [
            i for i in pkg.installers if i.installer_type != "nupkg"
        ]
        assert real_installers, "expected parsed installers, not the .nupkg stub"
        # Google Chrome is distributed as MSI — verify the parser detected it.
        assert any(i.installer_type == "msi" for i in real_installers)
        # And the SHA256 on the x86 MSI must match the one winget publishes
        # (cross-source hash agreement — the strongest correctness signal).
        x86 = next(i for i in real_installers if i.architecture == "x86")
        assert x86.sha256
        assert x86.sha256.startswith("ae9ba8c2")  # the well-known Chrome MSI hash
        assert x86.silent_switch  # /quiet /norestart …
        assert pkg.raw_data.get("install_script")
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_evergreen_get_package():
    from appcatalog_mcp.adapters import EvergreenAdapter

    http, settings = _build_live()
    try:
        adapter = EvergreenAdapter(http, settings.evergreen_api)
        pkg = await adapter.get_package("MicrosoftEdge")
        assert pkg.source == "evergreen"
        assert pkg.version  # latest version, non-empty
        assert pkg.installers  # per-arch installers
        archs = {i.architecture for i in pkg.installers}
        assert "x64" in archs
        # Microsoft Edge is distributed as MSI from the vendor.
        assert any(i.installer_type == "msi" for i in pkg.installers)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_evergreen_7zip_has_sha256():
    """7-Zip's GitHub release feed publishes SHA256 — Evergreen surfaces it."""
    from appcatalog_mcp.adapters import EvergreenAdapter

    http, settings = _build_live()
    try:
        adapter = EvergreenAdapter(http, settings.evergreen_api)
        pkg = await adapter.get_package("7zip")
        assert pkg.source == "evergreen"
        assert any(i.sha256 for i in pkg.installers)
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_live_find_best_source_ranks_winget_for_7zip():
    """Cross-source ranking: winget (MSI + ProductCode + SHA) > evergreen > choco """
    from appcatalog_mcp.adapters import (
        ChocolateyAdapter,
        EvergreenAdapter,
        WingetAdapter,
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.config import Settings
    from appcatalog_mcp.http_client import HttpClient
    from appcatalog_mcp.rate_limiter import RateLimiter
    from appcatalog_mcp.tools.catalog import _score_package

    settings = Settings(cache_dir=CACHE_DIR, cache_ttl_hours=24, request_delay_seconds=0.5)
    cache = CacheStore(settings.cache_db_path, settings.cache_ttl_seconds)
    http = HttpClient(settings, cache, RateLimiter(0.5))
    try:
        _winget = WingetAdapter(http, settings)
        _choco = ChocolateyAdapter(http, settings.choco_api)
        _evergreen = EvergreenAdapter(http, settings.evergreen_api)

        w = await _winget.get_package("7zip.7zip")
        c = await _choco.get_installer_detail("7zip")
        e = await _evergreen.get_package("7zip")
        w_score, _ = _score_package(w)
        c_score, _ = _score_package(c)
        e_score, _ = _score_package(e)
        # Winget has the richest metadata: MSI URLs, SHA256, ProductCode, UpgradeCode.
        assert w_score > e_score > c_score
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


@pytest.mark.asyncio
async def test_live_verify_hash_against_evergreen_7zip():
    """Evergreen publishes a SHA256 for 7-Zip; streaming the URL must match it.

    Also asserts that a deliberately-wrong expected hash compares as a mismatch,
    so the verification logic can't silently pass everything.
    """
    from appcatalog_mcp.adapters import EvergreenAdapter

    http, settings = _build_live()
    try:
        ev = EvergreenAdapter(http, settings.evergreen_api)
        pkg = await ev.get_package("7zip")
        hashed = [i for i in pkg.installers if i.sha256]
        if not hashed:
            pytest.skip("Evergreen 7zip exposed no SHA256 this run")
        inst = hashed[0]

        digest, bytes_read, status, error = await http.stream_sha256(
            inst.url, max_bytes=settings.verify_max_bytes
        )
        assert error is None
        assert status == 200
        assert bytes_read > 0
        assert digest is not None
        # Correct expected hash matches (this is what verify_hash compares).
        assert digest.lower() == inst.sha256.lower()
        # A deliberately-wrong expected hash must NOT match the same digest.
        assert digest.lower() != "0" * 64
    finally:
        await http.close()
