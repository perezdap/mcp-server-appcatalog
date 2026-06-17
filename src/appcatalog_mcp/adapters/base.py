"""ABC + common types for package source adapters."""

from __future__ import annotations

import abc
from typing import Any

from appcatalog_mcp.http_client import HttpClient
from appcatalog_mcp.models import PackageMetadata


class PackageNotFoundError(Exception):
    """Raised when a package is not present in a source."""


class PackageAdapter(abc.ABC):
    """Abstract base class for a package repository adapter.

    Each source implements the same interface so MCP tools can query multiple
    sources uniformly. Implementations map source-specific payloads to the
    shared :class:`PackageMetadata` model via ``normalize``.
    """

    name: str = "abstract"
    display_label: str = "abstract"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @property
    def cache_prefix(self) -> str:
        return self.name

    # ---- Capability methods ------------------------------------------------
    @abc.abstractmethod
    async def search(
        self, query: str, *, limit: int = 10
    ) -> list[PackageMetadata]:
        """Search packages by keyword; return latest-version summaries."""

    @abc.abstractmethod
    async def get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        """Get full metadata for a specific package; latest if version is None."""

    @abc.abstractmethod
    async def list_recent(self, *, limit: int = 10) -> list[PackageMetadata]:
        """Return recently updated packages."""

    async def get_manifest(self, package_id: str, version: str | None = None) -> dict[str, Any]:
        """Return raw source manifest (best-effort). Adapters may override."""
        raise NotImplementedError(f"{self.name} does not expose raw manifests")

    async def get_installer_detail(
        self, package_id: str, version: str | None = None
    ) -> PackageMetadata:
        """Return a package record optimized for installer metadata.

        Default: fall back to ``get_package``. Adapters with richer installer
        data (e.g. winget installer manifest) may override.
        """
        return await self.get_package(package_id, version=version)

    @staticmethod
    @abc.abstractmethod
    def normalize(raw: Any) -> PackageMetadata:
        """Map a raw source payload to a normalized PackageMetadata model."""

    # ---- Cache helpers -----------------------------------------------------
    def cache_key(self, suffix: str) -> str:
        return f"{self.cache_prefix}:{suffix}"
