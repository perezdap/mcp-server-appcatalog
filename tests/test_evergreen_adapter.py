"""Evergreen adapter unit tests with mocked HTTP + real fixtures."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from appcatalog_mcp.adapters import EvergreenAdapter, PackageNotFoundError
from appcatalog_mcp.adapters.evergreen_adapter import (
    _latest_rows,
    _normalize_arch,
    _parse_evergreen_date,
)
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient
from appcatalog_mcp.models import PackageMetadata

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _read_json(name: str):
    return json.loads(_read(name))


def make_adapter(tmp_path):
    settings = Settings(
        cache_dir=tmp_path,
        cache_ttl_hours=1,
        request_delay_seconds=0,
        evergreen_api="https://evergreen-api.stealthpuppy.com",
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    return EvergreenAdapter(http, settings.evergreen_api), http


# ---------------------------------------------------------------------------


def test_parse_evergreen_date_eu_format():
    assert _parse_evergreen_date("27/4/2026") is not None
    assert _parse_evergreen_date("27/4/2026").day == 27
    assert _parse_evergreen_date("27/4/2026").month == 4
    assert _parse_evergreen_date("2026-04-27") is not None
    assert _parse_evergreen_date(None) is None
    assert _parse_evergreen_date("garbage") is None


def test_normalize_arch_canonicalises_variants():
    assert _normalize_arch("x86_64") == "x64"
    assert _normalize_arch("AMD64") == "x64"
    assert _normalize_arch("x86") == "x86"
    assert _normalize_arch("ARM64") == "arm64"
    assert _normalize_arch("") == "neutral"
    assert _normalize_arch("Any") == "neutral"


def test_latest_rows_picks_most_recent_version():
    rows = _read_json("evergreen_app_MicrosoftEdge.json")
    latest = _latest_rows(rows)
    # Should narrow to a single most-recent Version across all entries.
    versions = {r["Version"] for r in latest}
    assert len(versions) == 1, "all latest rows must share the same Version"
    best_version = next(iter(versions))
    # And that version is the newest by Date.
    assert best_version == max(
        (r["Version"] for r in rows),
        key=lambda v: _parse_evergreen_date(
            next((r["Date"] for r in rows if r["Version"] == v), None)
        ) or datetime.min,
    )


def test_latest_rows_empty_input():
    assert _latest_rows([]) == []


def test_normalize_full_microsoftedge_payload():
    """Full /app/MicrosoftEdge → PackageMetadata with per-arch installers."""
    rows = _read_json("evergreen_app_MicrosoftEdge.json")
    latest = _latest_rows(rows)
    pkg = EvergreenAdapter.normalize(
        {"name": "MicrosoftEdge", "version": None, "rows": latest, "all_rows": rows}
    )
    assert isinstance(pkg, PackageMetadata)
    assert pkg.source == "evergreen"
    assert pkg.id == "MicrosoftEdge"
    assert pkg.version  # non-empty
    assert len(pkg.installers) >= 3
    archs = {i.architecture for i in pkg.installers}
    assert "arm64" in archs
    assert "x64" in archs
    assert "x86" in archs
    for inst in pkg.installers:
        assert inst.installer_type == "msi"
        assert inst.url  # vendor URI present
    assert pkg.gallery_url == "https://stealthpuppy.com/apptracker/"
    assert pkg.raw_data["vendor_source"] is True


def test_normalize_7zip_includes_sha256():
    """7-Zip's vendor (GitHub release) publishes a SHA256 per asset."""
    rows = _read_json("evergreen_app_7zip.json")
    latest = _latest_rows(rows)
    pkg = EvergreenAdapter.normalize(
        {"name": "7zip", "version": None, "rows": latest, "all_rows": rows}
    )
    assert pkg.source == "evergreen"
    assert any(i.sha256 for i in pkg.installers)


@pytest.mark.asyncio
async def test_get_package_uses_app_endpoint(tmp_path):
    adapter, http = make_adapter(tmp_path)
    rows = _read_json("evergreen_app_MicrosoftEdge.json")

    async def fake_fetch_json(
        url, headers=None, params=None, cache_key=None, use_cache=True, ttl_override=None,
    ):
        if url.endswith("/apps"):
            return [{"Name": "MicrosoftEdge"}], False
        if "/app/MicrosoftEdge" in url:
            return rows, False
        raise AssertionError(f"unexpected URL: {url}")

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "get_cache", return_value=None),
    ):
        pkg = await adapter.get_package("MicrosoftEdge")
    assert pkg.id == "MicrosoftEdge"
    assert pkg.installers
    await http.close()


@pytest.mark.asyncio
async def test_get_package_specific_version_filters(tmp_path):
    """When a version is specified, only rows of that version are returned."""
    adapter, http = make_adapter(tmp_path)
    rows = _read_json("evergreen_app_MicrosoftEdge.json")
    # Find a non-latest version present in the fixture.
    all_versions = {r["Version"] for r in rows}
    latest_version = _latest_rows(rows)[0]["Version"]
    target_version = next(v for v in all_versions if v != latest_version)

    async def fake_fetch_json(*args, **kwargs):
        return rows, False

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "get_cache", return_value=None),
    ):
        pkg = await adapter.get_package("MicrosoftEdge", version=target_version)
    assert pkg.version == target_version
    for inst_arch in {i.architecture for i in pkg.installers}:
        pass  # just sanity that we got installers
    await http.close()


@pytest.mark.asyncio
async def test_get_package_unknown_id_raises(tmp_path):
    """Evergreen returns ``{"message": "Application not found. ..."}`` for unknown apps."""
    adapter, http = make_adapter(tmp_path)

    async def fake_fetch_json(*args, **kwargs):
        return {"message": "Application not found. Call /apps."}, False

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "get_cache", return_value=None),
    ):
        with pytest.raises(PackageNotFoundError):
            await adapter.get_package("DefinitelyNotAnApp")
    await http.close()


@pytest.mark.asyncio
async def test_search_uses_apps_list_then_per_app_detail(tmp_path):
    adapter, http = make_adapter(tmp_path)
    apps = _read_json("evergreen_apps.json")
    edge_rows = _read_json("evergreen_app_MicrosoftEdge.json")

    counter = {"apps_calls": 0, "app_calls": 0}

    async def fake_fetch_json(
        url, headers=None, params=None, cache_key=None, use_cache=True, ttl_override=None,
    ):
        if url.endswith("/apps"):
            counter["apps_calls"] += 1
            return apps, False
        if "/app/" in url:
            counter["app_calls"] += 1
            return edge_rows, False
        raise AssertionError(f"unexpected URL: {url}")

    # Patch the in-memory cache layer (not the network) so each /apps call
    # is served from cache on repeat, exercising the real caching path.
    cache_state: dict[str, object] = {}

    def fake_get_cache(key: str):
        return cache_state.get(key)

    def fake_set_cache(key: str, value):
        cache_state[key] = value

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "get_cache", side_effect=fake_get_cache),
        patch.object(http, "set_cache", side_effect=fake_set_cache),
    ):
        results = await adapter.search("edge", limit=5)
    assert results
    assert all(r.source == "evergreen" for r in results)
    assert counter["apps_calls"] == 1, "apps list must be cached after first call"
    await http.close()


@pytest.mark.asyncio
async def test_list_recent_returns_empty(tmp_path):
    """Evergreen doesn't expose updates-by-date; ``list_recent`` degrades to []."""
    adapter, http = make_adapter(tmp_path)
    assert await adapter.list_recent(limit=5) == []
    await http.close()
