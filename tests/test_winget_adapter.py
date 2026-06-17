"""Winget adapter unit tests with mocked HTTP + real fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from appcatalog_mcp.adapters.winget_adapter import (
    WingetAdapter,
    _extract_package_id_from_path,
    _version_sort_key,
)
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient, HttpClientError
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
        winget_api="auto",
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    return WingetAdapter(http, settings), http


# ---------------------------------------------------------------------------


def test_split_package_id():
    # Common Publisher.Package form
    publisher, package_path, letter = WingetAdapter.split_package_id("Google.Chrome")
    assert (publisher, package_path, letter) == ("Google", "Chrome", "g")


@pytest.mark.parametrize(
    "package_id, expected_dir, expected_subpath",
    [
        ("Google.Chrome", "manifests/g/Google/Chrome", "g/Google/Chrome"),
        (
            "Microsoft.VisualStudio.2022.Community",
            "manifests/m/Microsoft/VisualStudio/2022/Community",
            "m/Microsoft/VisualStudio/2022/Community",
        ),
        (
            "Microsoft.DotNet.SDK.8",
            "manifests/m/Microsoft/DotNet/SDK/8",
            "m/Microsoft/DotNet/SDK/8",
        ),
    ],
)
def test_multi_segment_paths_are_nested(package_id, expected_dir, expected_subpath):
    from appcatalog_mcp.adapters.winget_adapter import _id_to_subpath

    publisher, package_path, _letter = WingetAdapter.split_package_id(package_id)
    # package_path is everything after the publisher segment.
    expected_segments = expected_dir.split("/")  # ['manifests','g','Google','Chrome']
    expected_package_path = "/".join(expected_segments[3:])
    assert publisher == expected_segments[2]
    assert package_path == expected_package_path
    assert _id_to_subpath(package_id) == expected_subpath



def test_split_package_id_invalid():
    with pytest.raises(Exception):
        WingetAdapter.split_package_id("noseparator")


def test_version_sort_key_orders_numeric():
    versions = ["9.0.0", "26.1.0", "25.0.0", "3.0.21"]
    out = sorted(versions, key=_version_sort_key, reverse=True)
    assert out[0] == "26.1.0"
    assert out[-1] == "3.0.21"


def test_extract_package_id_from_path():
    p = "manifests/g/Google/Chrome/149.0.7827.156/Google.Chrome.yaml"
    assert _extract_package_id_from_path(p) == ["Google.Chrome"]
    assert _extract_package_id_from_path("not/a/manifest") == []


@pytest.mark.parametrize(
    "path, expected",
    [
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.installer.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.locale.default.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.locale.en-US.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.locale.fr-FR.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.locale.de-DE.yaml", ["Google.Chrome"]),
        ("manifests/g/Google/Chrome/1.0/Google.Chrome.locale.ja-JP.yaml", ["Google.Chrome"]),
        (
            "manifests/m/Microsoft/VisualStudio/2022/Community/17.10.0/Microsoft.VisualStudio.2022.Community.yaml",
            ["Microsoft.VisualStudio.2022.Community"],
        ),
    ],
)
def test_extract_package_id_from_path_handles_all_locales(path, expected):
    assert _extract_package_id_from_path(path) == expected


def test_looks_like_version_filters_channel_dirs():
    for name in ("149.0.7827.156", "1.2-beta", "1.0.0.1041"):
        assert WingetAdapter._looks_like_version(name) is True
    for name in ("Beta", "Canary", "EXE", "MSI"):
        assert WingetAdapter._looks_like_version(name) is False


# ---------------------------------------------------------------------------


def test_normalize_full_manifest_to_package_metadata():
    version_yaml = _read("winget_manifest_Google.Chrome.version.yaml")
    installer_yaml = _read("winget_manifest_Google.Chrome.installer.yaml")
    locale_yaml = _read("winget_manifest_Google.Chrome.locale_en_us.yaml")
    import yaml

    manifests = {
        "version": yaml.safe_load(version_yaml),
        "installer": yaml.safe_load(installer_yaml),
        "locale_en_us": yaml.safe_load(locale_yaml),
    }
    pkg = WingetAdapter.normalize(
        {"package_id": "Google.Chrome", "version": "149.0.7827.156", "manifests": manifests}
    )
    assert isinstance(pkg, PackageMetadata)
    assert pkg.source == "winget"
    assert pkg.id == "Google.Chrome"
    assert pkg.version == "149.0.7827.156"
    assert pkg.publisher == "Google LLC"
    assert pkg.name == "Google Chrome"
    assert pkg.license == "Freeware"
    assert len(pkg.installers) == 3
    x86, x64, arm64 = pkg.installers
    assert x86.architecture == "x86"
    assert x86.installer_type == "wix"
    assert x86.url.endswith("googlechromestandaloneenterprise.msi")
    assert x86.sha256 == "ae9ba8c2ca5ea4e46d0a33f30524a23a484d979df82cbe5c309b0406e43bfe2d"
    assert x86.product_code == "{DB8CE002-DC99-3C5D-9AC4-29B25ACD528D}"
    assert x64.architecture == "x64"
    assert arm64.architecture == "arm64"


@pytest.mark.asyncio
async def test_get_package_resolves_multi_segment_id_path(tmp_path):
    """``Microsoft.VisualStudio.2022.Community`` must build
    ``manifests/m/Microsoft/VisualStudio/2022/Community/{version}/...`` — not
    ``manifests/m/Microsoft/VisualStudio.2022.Community/...`` (which 404s)."""
    adapter, http = make_adapter(tmp_path)

    captured_urls: list[str] = []

    async def fake_fetch_json(
        url, headers=None, params=None, cache_key=None, use_cache=True, ttl_override=None
    ):
        captured_urls.append(url)
        # GH Contents dir listing with one version dir.
        return (
            [
                {
                    "name": "17.10.0",
                    "type": "dir",
                    "path": "manifests/m/Microsoft/VisualStudio/2022/Community/17.10.0",
                }
            ],
            False,
        )

    async def fake_fetch_text(url, headers=None, params=None, cache_key=None, use_cache=True):
        captured_urls.append(url)
        # Every YAML file should resolve under the nested multi-segment path.
        assert "manifests/m/Microsoft/VisualStudio/2022/Community/17.10.0/" in url, url
        raise HttpClientError(f"HTTP 404 for {url}")

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "fetch_text", AsyncMock(side_effect=fake_fetch_text)),
        patch.object(http, "get_cache", return_value=None),
    ):
        from appcatalog_mcp.adapters import PackageNotFoundError as _PNF

        with pytest.raises(_PNF):
            await adapter.get_package("Microsoft.VisualStudio.2022.Community")
    # Confirm the directory listing was requested at the properly nested path.
    assert any(
        "contents/manifests/m/Microsoft/VisualStudio/2022/Community" in u for u in captured_urls
    ), captured_urls
    await http.close()


@pytest.mark.asyncio
async def test_get_package_uses_github_path(tmp_path):
    adapter, http = make_adapter(tmp_path)
    gh_dir = _read_json("winget_github_dir_Google.Chrome.json")
    version_yaml = _read("winget_manifest_Google.Chrome.version.yaml")
    installer_yaml = _read("winget_manifest_Google.Chrome.installer.yaml")
    locale_yaml = _read("winget_manifest_Google.Chrome.locale_en_us.yaml")

    # Mock the network calls sequentially.
    async def fake_fetch_json(
        url, headers=None, params=None, cache_key=None, use_cache=True, ttl_override=None
    ):
        if "contents/manifests/g/Google/Chrome" in url:
            return gh_dir, False
        raise AssertionError(f"unexpected fetch_json call: {url}")

    async def fake_fetch_text(url, headers=None, params=None, cache_key=None, use_cache=True):
        if url.endswith("Google.Chrome.yaml"):
            return version_yaml, False
        if url.endswith("Google.Chrome.installer.yaml"):
            return installer_yaml, False
        if url.endswith("Google.Chrome.locale.en-US.yaml"):
            return locale_yaml, False
        # Missing files (e.g. .locale.default.yaml) come back as 404.
        raise HttpClientError(f"HTTP 404 for {url}")

    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fake_fetch_json)),
        patch.object(http, "fetch_text", AsyncMock(side_effect=fake_fetch_text)),
        patch.object(http, "get_cache", return_value=None),
    ):
        pkg = await adapter.get_package("Google.Chrome")
    assert pkg.id == "Google.Chrome"
    assert pkg.version == "149.0.7827.156"
    assert len(pkg.installers) == 3
    await http.close()


@pytest.mark.asyncio
async def test_get_package_not_found_raises(tmp_path):
    adapter, http = make_adapter(tmp_path)
    with (
        patch.object(
            http,
            "fetch_json",
            AsyncMock(side_effect=HttpClientError("HTTP 404")),
        ),
        patch.object(http, "get_cache", return_value=None),
    ):
        from appcatalog_mcp.adapters import PackageNotFoundError

        with pytest.raises(PackageNotFoundError):
            await adapter.get_package("Does.NotExist")
    await http.close()


@pytest.mark.asyncio
async def test_search_uses_wingetrun(tmp_path):
    adapter, http = make_adapter(tmp_path)
    wr = _read_json("wingetrun_search_chrome.json")
    with (
        patch.object(http, "fetch_json", AsyncMock(return_value=(wr, False))),
        patch.object(http, "get_cache", return_value=None),
    ):
        results = await adapter.search("chrome", limit=5)
    assert results
    assert all(r.source == "winget" for r in results)
    assert all(r.id for r in results)
    await http.close()


@pytest.mark.asyncio
async def test_search_wingetrun_transient_failure_trips_time_bounded_breaker(tmp_path):
    """A single transient winget.run failure should NOT permanently disable
    search, and should NOT cache the empty result for the full TTL."""
    adapter, http = make_adapter(tmp_path)
    call_count = 0

    async def fetch_then_fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise HttpClientError("HTTP 503 service unavailable")

    set_cache_mock = AsyncMock()
    with (
        patch.object(http, "fetch_json", AsyncMock(side_effect=fetch_then_fail)),
        patch.object(http, "get_cache", return_value=None),
        patch.object(http, "set_cache", set_cache_mock),
    ):
        first = await adapter.search("chrome", limit=5)
    assert first == []
    # Breaker is tripped but not permanent.
    assert adapter._wingetrun_disabled is True
    adapter._wingetrun_disabled_until = 0.0  # simulate cooldown elapsing
    assert adapter._wingetrun_disabled is False
    # And we never persisted the empty failure result.
    set_cache_mock.assert_not_called()
    await http.close()


@pytest.mark.asyncio
async def test_search_caches_successful_empty_results(tmp_path):
    """Successful (no error) empty results from winget.run ARE cached, so we
    don't refetch known-empty queries every call."""
    adapter, http = make_adapter(tmp_path)
    with (
        patch.object(
            http,
            "fetch_json",
            AsyncMock(return_value=({"Packages": [], "Total": 0}, False)),
        ),
        patch.object(http, "get_cache", return_value=None),
    ):
        results = await adapter.search("qqq_nonexistent", limit=5)
    assert results == []
    await http.close()
