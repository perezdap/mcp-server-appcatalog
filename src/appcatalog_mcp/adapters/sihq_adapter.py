"""Silent Install HQ adapter.

Calls the separately-deployed ``mcp-server-silentinstallhq`` MCP server over
streamable-http using the MCP Python SDK client. Used to fall back when silent
install switches aren't present in the winget manifest itself.

If the SIHQ server is unreachable or unconfigured, methods return ``None``
(graceful degradation) so callers can surface raw manifest switches only.
"""

from __future__ import annotations

import logging
from typing import Any

from appcatalog_mcp.adapters.base import PackageAdapter, PackageNotFoundError
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import PackageMetadata

logger = logging.getLogger(__name__)


class SihqAdapter(PackageAdapter):
    """Adapter that delegates to the silentinstallhq MCP server."""

    name = "silentinstallhq"
    display_label = "Silent Install HQ"

    def __init__(self, http: HttpClient, sihq_endpoint: str) -> None:
        super().__init__(http)
        self.endpoint = sihq_endpoint
        # Import lazily so the rest of the server can run without mcp client
        # tooling; we only need it when actually delegating switches.
        self._client_lib: Any = None

    async def _call_extract_switches(self, software_name: str) -> dict[str, Any] | None:
        """Open a short-lived MCP streamable-http client session and call the tool."""
        if not self.endpoint:
            logger.debug("SIHQ endpoint not configured; skipping fallback")
            return None
        try:
            try:
                from mcp import ClientSession
                from mcp.client.streamable_http import (
                    streamable_http_client as _streamable_http_client,
                )
            except ImportError as exc:  # pragma: no cover - environment-dependent
                logger.error("mcp client SDK unavailable for SIHQ delegation: %s", exc)
                return None

            try:
                async with _streamable_http_client(self.endpoint) as (
                    read,
                    write,
                    _getting_session_id,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "extract_switches",
                            {"software_name": software_name},
                        )
                        return _coerce_tool_result(result)
            except Exception as exc:  # broad: any transport/protocol failure
                logger.warning("SIHQ extract_switches call failed: %s", exc)
                return None
        except HttpClientError as exc:
            logger.warning("SIHQ HTTP failure: %s", exc)
            return None

    # ---- PackageAdapter interface (best-effort) ----------------------------
    async def search(
        self, query: str, *, limit: int = 10
    ) -> list[PackageMetadata]:
        # SIHQ is not a package repository; searching it returns no packages.
        return []

    async def get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        raise PackageNotFoundError(
            "Silent Install HQ is a silent-switch source, not a package catalog"
        )

    async def list_recent(self, *, limit: int = 10) -> list[PackageMetadata]:
        return []

    @staticmethod
    def normalize(raw: Any) -> PackageMetadata:  # pragma: no cover - not used
        raise NotImplementedError("SihqAdapter does not produce PackageMetadata")


def _coerce_tool_result(result: Any) -> dict[str, Any] | None:
    """Pull a structured dict out of an MCP CallToolResult.

    FastMCP returns content in ``result.content`` (list of TextContent) and
    structured data in ``result.structuredContent`` when the tool declares an
    output schema. We tolerate either shape.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "result" in structured and isinstance(structured["result"], dict):
            return structured["result"]
        return structured

    content = getattr(result, "content", None)
    if content:
        for item in content:
            text = getattr(item, "text", None)
            if not text:
                continue
            import json

            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None
