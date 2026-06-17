"""Unit tests for HttpClient.stream_sha256 (the verify_hash engine).

These mock httpx's streaming response so they run fully offline. The tool
wrapper in tools/catalog.py is a thin shell over this method, so verifying the
streaming/cap/error behavior here covers the meaningful logic.
"""

from __future__ import annotations

import hashlib

from appcatalog_mcp.cache import CacheStore
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient
from appcatalog_mcp.rate_limiter import RateLimiter


def _make_http(tmp_path) -> HttpClient:
    settings = Settings(cache_dir=tmp_path, cache_ttl_hours=1, request_delay_seconds=0)
    cache = CacheStore(tmp_path / "cache.sqlite", 60)
    return HttpClient(settings, cache, RateLimiter(0))


class _FakeStreamResponse:
    """Mimics the async context manager returned by httpx.AsyncClient.stream."""

    def __init__(
        self, chunks: list[bytes], status_code: int = 200, headers: dict | None = None
    ) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def _patch_stream(http: HttpClient, chunks: list[bytes], status_code: int = 200) -> None:
    def _stream(method, url, headers=None, follow_redirects=False):
        return _FakeStreamResponse(chunks, status_code=status_code)

    http._client.stream = _stream  # type: ignore[assignment]


async def test_stream_sha256_matches_known_payload(tmp_path):
    http = _make_http(tmp_path)
    payload = b"hello world installer bytes"
    expected = hashlib.sha256(payload).hexdigest()
    # split into chunks to exercise incremental hashing
    _patch_stream(http, [payload[:5], payload[5:15], payload[15:]])

    digest, bytes_read, status, error = await http.stream_sha256(
        "https://example.test/app.bin", max_bytes=1_000_000
    )

    assert digest == expected
    assert bytes_read == len(payload)
    assert status == 200
    assert error is None
    await http.close()


async def test_stream_sha256_size_limit_exceeded(tmp_path):
    http = _make_http(tmp_path)
    _patch_stream(http, [b"a" * 100, b"b" * 100])

    digest, bytes_read, status, error = await http.stream_sha256(
        "https://example.test/big.bin", max_bytes=150
    )

    assert digest is None
    assert error == "size_limit_exceeded"
    assert bytes_read > 150
    await http.close()


async def test_stream_sha256_http_error_status(tmp_path):
    http = _make_http(tmp_path)
    _patch_stream(http, [], status_code=404)

    digest, bytes_read, status, error = await http.stream_sha256(
        "https://example.test/missing.bin", max_bytes=1_000_000
    )

    assert digest is None
    assert status == 404
    assert error == "HTTP 404"
    assert bytes_read == 0
    await http.close()


async def test_stream_sha256_network_failure(tmp_path):
    import httpx

    http = _make_http(tmp_path)

    def _boom(method, url, headers=None, follow_redirects=False):
        raise httpx.ConnectError("boom")

    http._client.stream = _boom  # type: ignore[assignment]

    digest, bytes_read, status, error = await http.stream_sha256(
        "https://example.test/x.bin", max_bytes=1_000_000
    )

    assert digest is None
    assert status == 0
    assert error is not None and "boom" in error
    await http.close()


def test_verify_max_bytes_setting_default(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    assert settings.verify_max_bytes == 500 * 1024 * 1024


def test_hash_verification_model_round_trip():
    from appcatalog_mcp.models import HashVerification

    hv = HashVerification(
        url="https://example.test/app.msi",
        expected_sha256="abc",
        computed_sha256="abc",
        match=True,
        bytes_read=1234,
        elapsed_ms=42,
        status_code=200,
    )
    dumped = hv.model_dump()
    assert dumped["match"] is True
    assert HashVerification.model_validate(dumped).computed_sha256 == "abc"


def test_hash_matches_case_insensitive():
    from appcatalog_mcp.tools.catalog import _hash_matches

    assert _hash_matches("ABC123", "abc123") is True
    assert _hash_matches("  abc123  ", "ABC123") is True


def test_hash_matches_rejects_mismatch_and_missing():
    from appcatalog_mcp.tools.catalog import _hash_matches

    # The core verify_hash behavior: a wrong hash must NOT match.
    assert _hash_matches("abc123", "def456") is False
    # A failed download (None digest) must never report a match.
    assert _hash_matches("abc123", None) is False
    assert _hash_matches("abc123", "") is False


def test_host_is_blocked_for_loopback_and_private():
    from appcatalog_mcp.http_client import _host_is_blocked

    # Literal IPs resolve to themselves via getaddrinfo.
    assert _host_is_blocked("127.0.0.1") is True
    assert _host_is_blocked("169.254.169.254") is True  # cloud metadata
    assert _host_is_blocked("10.0.0.5") is True
    assert _host_is_blocked("192.168.1.1") is True
    assert _host_is_blocked("::1") is True
    assert _host_is_blocked("") is True


def test_host_is_blocked_allows_public():
    from appcatalog_mcp.http_client import _host_is_blocked

    assert _host_is_blocked("8.8.8.8") is False
    assert _host_is_blocked("1.1.1.1") is False


async def test_stream_sha256_blocks_private_host(tmp_path):
    http = _make_http(tmp_path)
    # No stream patch needed: the guard fires before any request.
    digest, bytes_read, status, error = await http.stream_sha256(
        "http://127.0.0.1:8080/secret", max_bytes=1_000_000, block_private_hosts=True
    )
    assert digest is None
    assert error == "blocked_private_host"
    assert bytes_read == 0
    await http.close()


async def test_stream_sha256_allows_private_host_when_disabled(tmp_path):
    http = _make_http(tmp_path)
    payload = b"internal-ok"
    _patch_stream(http, [payload])
    digest, _, status, error = await http.stream_sha256(
        "http://127.0.0.1:8080/x", max_bytes=1_000_000, block_private_hosts=False
    )
    assert error is None
    assert digest == hashlib.sha256(payload).hexdigest()
    await http.close()
