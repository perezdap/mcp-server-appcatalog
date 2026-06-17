"""Async HTTP client with caching, rate limiting, and polite headers."""

from __future__ import annotations

import io
import logging
from typing import Any

import httpx

from appcatalog_mcp.cache import CacheStore
from appcatalog_mcp.config import Settings
from appcatalog_mcp.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class HttpClientError(Exception):
    """Raised on any HTTP fetch failure (network, status, timeout)."""


class HttpClient:
    """Thin wrapper around httpx with SQLite cache + global rate limiting.

    Provides ``fetch_json`` and ``fetch_text``. Cache is keyed by URL + extras.
    Network calls are paced by the shared RateLimiter and identifiable via the
    configured User-Agent.
    """

    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        rate_limiter: RateLimiter,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.rate_limiter = rate_limiter
        limits = httpx.Limits(
            max_connections=settings.httpx_max_connections,
            max_keepalive_connections=settings.httpx_max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            follow_redirects=True,
            limits=limits,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        await self.rate_limiter.acquire()
        logger.info("HTTP GET %s params=%s", url, params)
        try:
            response = await self._client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise HttpClientError(f"Request failed for {url}: {exc}") from exc
        if response.status_code == 404:
            raise HttpClientError(f"HTTP 404 for {url}")
        if response.status_code == 429:
            raise HttpClientError(
                f"HTTP 429 rate limited for {url}; set GITHUB_TOKEN or raise request delay"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HttpClientError(f"HTTP {response.status_code} for {url}") from exc
        return response

    async def fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        use_cache: bool = True,
        ttl_override: int | None = None,
    ) -> tuple[Any, bool]:
        """Return JSON-decoded body and a boolean indicating a cache hit."""
        key = cache_key or self._json_cache_key(url, params)
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                logger.debug("JSON cache hit for %s", key)
                return cached, True

        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)

        response = await self._request(url, headers=merged_headers, params=params)
        data = response.json()

        if ttl_override is not None and ttl_override <= 0:
            # Explicitly skip caching for very volatile endpoints.
            self.cache.delete(key)
        else:
            self.cache.set(key, data)

        return data, False

    async def fetch_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        use_cache: bool = True,
    ) -> tuple[str, bool]:
        """Return response text (used for GitHub Contents API JSON, raw YAML)."""
        key = cache_key or f"text:{url}"
        if use_cache:
            cached = self.cache.get(key)
            if isinstance(cached, str):
                logger.debug("Text cache hit for %s", key)
                return cached, True

        response = await self._request(url, headers=headers or None, params=params)
        text = response.text
        self.cache.set(key, text)
        return text, False

    @staticmethod
    def _json_cache_key(url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return f"json:{url}"
        import hashlib
        import json as _json

        stable = _json.dumps(params, sort_keys=True, default=str)
        digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
        return f"json:{url}:{digest}"

    def set_cache(self, key: str, value: Any) -> None:
        self.cache.set(key, value)

    def get_cache(self, key: str) -> Any | None:
        return self.cache.get(key)

    async def fetch_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cache_key: str | None = None,
        use_cache: bool = True,
    ) -> tuple[bytes, bool]:
        """Return raw response bytes (used for binary downloads like .nupkg).

        Bytes are cached too, but callers should set a longer-lived cache key
        via ``cache_key`` and be mindful that large binaries bloat the SQLite DB.
        Returns ``(payload, cache_hit)``.
        """
        key = cache_key or f"bytes:{url}"
        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                # Cache stores JSON; bytes round-trip via latin-1 codec so the
                # payload survives ``json.dumps`` losslessly.
                if isinstance(cached, str):
                    return cached.encode("latin-1"), True
                if isinstance(cached, (bytes, bytearray)):
                    return bytes(cached), True

        response = await self._request(url, headers=headers or None)
        data = response.content
        if use_cache:
            try:
                self.cache.set(key, data.decode("latin-1"))
            except (UnicodeDecodeError, ValueError):
                # Extremely unlikely given latin-1 maps every byte 0x00-0xff;
                # if it ever fails we just skip caching for this payload.
                self.cache.delete(key)
        return data, False

    async def fetch_zip_member(
        self,
        url: str,
        member_path: str,
        *,
        headers: dict[str, str] | None = None,
        cache_key: str | None = None,
        use_cache: bool = True,
    ) -> bytes:
        """Fetch a zip archive over HTTP and read a single member without writing to disk.

        Used for Chocolatey ``.nupkg`` inspection: download the zip, open in
        memory with :mod:`zipfile`, return the named member's bytes. The full
        .nupkg is cached under ``cache_key`` (or ``bytes:{url}``) so repeated
        member reads don't re-fetch the same archive.
        """
        import zipfile

        archive_bytes, _ = await self.fetch_bytes(
            url,
            headers=headers,
            cache_key=cache_key,
            use_cache=use_cache,
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            try:
                return zf.read(member_path)
            except KeyError as exc:
                raise HttpClientError(f"Member {member_path!r} not found in {url}") from exc
