"""SIHQ adapter tests (no live server required)."""

from __future__ import annotations

import pytest

from appcatalog_mcp.adapters import PackageNotFoundError, SihqAdapter
from appcatalog_mcp.adapters.sihq_adapter import _coerce_tool_result
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient


def make_adapter(tmp_path):
    settings = Settings(
        cache_dir=tmp_path,
        cache_ttl_hours=1,
        request_delay_seconds=0,
        sihq_url="http://127.0.0.1:8000/mcp",
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    return SihqAdapter(http, settings.sihq_url), http


@pytest.mark.asyncio
async def test_sihq_search_returns_empty(tmp_path):
    """SIHQ is a silent-switch source, not a package catalog."""
    adapter, http = make_adapter(tmp_path)
    assert await adapter.search("chrome") == []
    await http.close()


@pytest.mark.asyncio
async def test_sihq_get_package_not_supported(tmp_path):
    adapter, http = make_adapter(tmp_path)
    with pytest.raises(PackageNotFoundError):
        await adapter.get_package("Google.Chrome")
    await http.close()


@pytest.mark.asyncio
async def test_sihq_list_recent_returns_empty(tmp_path):
    adapter, http = make_adapter(tmp_path)
    assert await adapter.list_recent(limit=5) == []
    await http.close()


@pytest.mark.asyncio
async def test_sihq_call_returns_none_without_endpoint(tmp_path):
    """No SIHQ URL configured → graceful None."""
    settings = Settings(
        cache_dir=tmp_path,
        cache_ttl_hours=1,
        request_delay_seconds=0,
        sihq_url="",
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    adapter = SihqAdapter(http, settings.sihq_url)
    assert await adapter._call_extract_switches("Cisco AnyConnect") is None
    await http.close()


@pytest.mark.asyncio
async def test_sihq_call_returns_none_when_endpoint_unreachable(tmp_path):
    """Live server not present on bogus ephemeral port → graceful None."""
    settings = Settings(
        cache_dir=tmp_path,
        cache_ttl_hours=1,
        request_delay_seconds=0,
        sihq_url="http://127.0.0.1:1/mcp",  # unroutable
        request_timeout_seconds=2,
    )
    from appcatalog_mcp.cache import CacheStore
    from appcatalog_mcp.rate_limiter import RateLimiter

    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    rl = RateLimiter(0)
    http = HttpClient(settings, cache, rl)
    adapter = SihqAdapter(http, settings.sihq_url)
    assert await adapter._call_extract_switches("7-Zip") is None
    await http.close()


# ---- _coerce_tool_result helper --------------------------------------------


def _text_content(text: str):
    class _C:
        def __init__(self, t): self.text = t
    return _C(text)


def test_coerce_result_prefers_structured_content():
    class _R:
        structuredContent = {"silent_install_switch": "/S"}
        content = [_text_content('{"silent_install_switch": "/Q"}')]
    assert _coerce_tool_result(_R()) == {"silent_install_switch": "/S"}


def test_coerce_result_falls_back_to_text_content_json():
    class _R:
        structuredContent = None
        content = [_text_content('{"silent_install_switch": "/passive"}')]
    assert _coerce_tool_result(_R()) == {"silent_install_switch": "/passive"}


def test_coerce_result_ignores_invalid_text():
    class _R:
        structuredContent = None
        content = [_text_content("not json"), _text_content('{"x": 1}')]
    assert _coerce_tool_result(_R()) == {"x": 1}


def test_coerce_result_returns_none_for_empty():
    class _R:
        structuredContent = None
        content = []
    assert _coerce_tool_result(_R()) is None
