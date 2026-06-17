"""Chocolatey adapter unit tests with mocked HTTP + real fixtures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient
from appcatalog_mcp.models import PackageMetadata

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_adapter(tmp_path):
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    settings = Settings(
        cache_dir=tmp_path,
        cache_ttl_hours=1,
        request_delay_seconds=0,
    )
    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    return ChocolateyAdapter(http, settings.choco_api), http


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_feed_recovers_truncated_xml():
    """choco_search_vlc.xml ends with a stray <m:error> after the feed."""
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter

    xml = _read("choco_search_vlc.xml")
    entries = ChocolateyAdapter._parse_feed(xml)
    assert len(entries) >= 4
    # First entry should be the vlc package itself.
    first = ChocolateyAdapter.normalize(entries[0])
    assert first.source == "chocolatey"
    assert first.id == "vlc"
    assert first.version == "3.0.23"
    assert first.name == "VLC media player"


@pytest.mark.asyncio
async def test_parse_versions_feed_clean():
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter

    xml = _read("choco_versions_7zip.xml")
    entries = ChocolateyAdapter._parse_feed(xml)
    assert len(entries) > 1
    first = ChocolateyAdapter.normalize(entries[0])
    assert first.id == "7zip"
    assert first.version == "26.1.0"
    assert first.publisher == "Igor Pavlov"
    assert first.dependencies and first.dependencies[0].id == "7zip.install"


def test_normalize_chocolatey_entry_full_fields():
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter

    xml = _read("choco_versions_7zip.xml")
    entries = ChocolateyAdapter._parse_feed(xml)
    pkg = ChocolateyAdapter.normalize(entries[0])
    assert isinstance(pkg, PackageMetadata)
    assert pkg.source == "chocolatey"
    assert pkg.installers[0].url == "https://community.chocolatey.org/api/v2/package/7zip/26.1.0"
    assert pkg.gallery_url.startswith("https://community.chocolatey.org/")
    # The .nupkg download URL is preserved on the single installer.
    assert pkg.installers[0].installer_type == "nupkg"
    assert pkg.homepage == "http://www.7-zip.org/"
    # SHA256 is intentionally None (Chocolatey uses SHA512, exposed in raw_data).
    assert pkg.installers[0].sha256 is None
    assert pkg.raw_data and pkg.raw_data["package_hash_algorithm"] == "SHA512"
    assert pkg.raw_data["package_hash"]  # base64 SHA512


def test_parse_dependencies_format():
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter

    deps = ChocolateyAdapter._parse_dependencies("7zip.install:[26.1.0]:|vlc.install:[3.0.23]:")
    assert deps[0].id == "7zip.install"
    assert deps[0].version == "26.1.0"
    assert deps[1].id == "vlc.install"


def test_parse_dependencies_empty():
    from appcatalog_mcp.adapters.chocolatey_adapter import ChocolateyAdapter

    assert ChocolateyAdapter._parse_dependencies("") == []
    assert ChocolateyAdapter._parse_dependencies(":::") == []


def test_odata_literal_doubles_single_quotes():
    from appcatalog_mcp.adapters.chocolatey_adapter import _odata_literal

    assert _odata_literal("7zip") == "7zip"
    assert _odata_literal("it's") == "it''s"
    assert _odata_literal("x' or '1' eq '1") == "x'' or ''1'' eq ''1"


@pytest.mark.asyncio
async def test_get_package_returns_latest_with_versions(tmp_path):
    adapter, http = make_adapter(tmp_path)
    latest_xml = _read("choco_latest_7zip.xml")
    versions_xml = _read("choco_versions_7zip.xml")

    async def fake_fetch_text(url, headers=None, params=None, cache_key=None, use_cache=True):
        if "IsLatestVersion eq true" in url and "tolower(Id) eq '7zip'" in url:
            return latest_xml, False
        return versions_xml, False

    with (
        patch.object(http, "fetch_text", AsyncMock(side_effect=fake_fetch_text)),
        patch.object(http, "get_cache", return_value=None),
    ):
        pkg = await adapter.get_package("7zip")
    assert pkg.id == "7zip"
    assert pkg.version == "26.1.0"
    assert "26.1.0" in pkg.versions
    assert pkg.versions[0] == "26.1.0"
    await http.close()


@pytest.mark.asyncio
async def test_search_returns_normalized_packages(tmp_path):
    adapter, http = make_adapter(tmp_path)
    search_xml = _read("choco_search_vlc.xml")
    with (
        patch.object(http, "fetch_text", AsyncMock(return_value=(search_xml, False))),
        patch.object(http, "get_cache", return_value=None),
    ):
        results = await adapter.search("vlc", limit=5)
    assert results
    assert all(r.source == "chocolatey" for r in results)
    assert any(r.id == "vlc" for r in results)
    await http.close()


@pytest.mark.asyncio
async def test_list_recent(tmp_path):
    adapter, http = make_adapter(tmp_path)
    recent_xml = _read("choco_recent.xml")
    with (
        patch.object(http, "fetch_text", AsyncMock(return_value=(recent_xml, False))),
        patch.object(http, "get_cache", return_value=None),
    ):
        results = await adapter.list_recent(limit=3)
    assert len(results) >= 1
    assert all(r.source == "chocolatey" for r in results)
    await http.close()


@pytest.mark.asyncio
async def test_get_package_not_found(tmp_path):
    from appcatalog_mcp.adapters import PackageNotFoundError

    adapter, http = make_adapter(tmp_path)
    empty_xml = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<title type="text">Packages</title>'
        '<id>x</id><updated>x</updated></feed>'
    )
    with (
        patch.object(http, "fetch_text", AsyncMock(return_value=(empty_xml, False))),
        patch.object(http, "get_cache", return_value=None),
    ):
        with pytest.raises(PackageNotFoundError):
            await adapter.get_package("does.not.exist")
    await http.close()
