"""Pydantic models for normalized package metadata and MCP tool responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceName = Literal["winget", "chocolatey", "silentinstallhq"]

UNKNOWN = "unknown"


class InstallerInfo(BaseModel):
    """A single installer artifact (URL, hash, arch, type, switches)."""

    url: str = Field(description="Direct download URL")
    sha256: str | None = Field(default=None, description="SHA256 hash for verification")
    installer_type: str = Field(
        default=UNKNOWN,
        description="exe, msi, msix, portable, zip, wix, nullsoft, etc.",
    )
    architecture: str = Field(default=UNKNOWN, description="x64, x86, arm64, arm, neutral")
    scope: str | None = Field(default=None, description="machine, user")
    product_code: str | None = Field(
        default=None, description="MSI/MSIX product code for detection"
    )
    upgrade_code: str | None = Field(default=None, description="MSI UpgradeCode")
    silent_switch: str | None = Field(default=None)
    silent_with_progress_switch: str | None = Field(default=None)
    file_size: int | None = Field(default=None, description="Bytes, if known")
    signature_sha256: str | None = Field(default=None)
    abi: str | None = Field(default=None, description="MSIX min/multiple ABI, if present")

    model_config = {"extra": "ignore"}


class VersionInfo(BaseModel):
    """A specific published version with its own installers."""

    version: str
    published: str | None = Field(default=None, description="ISO 8601 publish date")
    installers: list[InstallerInfo] = Field(default_factory=list)
    release_notes: str | None = Field(default=None)
    release_notes_url: str | None = Field(default=None)

    model_config = {"extra": "ignore"}


class DependencyInfo(BaseModel):
    """A Chocolatey/nuget dependency (id + version spec)."""

    id: str
    version: str | None = Field(default=None, description="Version range, e.g. [3.0.0, )")


class PackageMetadata(BaseModel):
    """Normalized package metadata returned by all tools."""

    source: SourceName
    id: str = Field(description="PackageIdentifier (winget) or Id (chocolatey)")
    name: str = Field(description="Package display name / Title")
    publisher: str = Field(default=UNKNOWN)
    version: str = Field(default=UNKNOWN)
    description: str = Field(default="")
    homepage: str | None = Field(default=None)
    license: str | None = Field(default=None)
    license_url: str | None = Field(default=None)
    release_notes_url: str | None = Field(default=None)
    release_notes: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)
    moniker: str | None = Field(default=None)
    update_date: str | None = Field(default=None, description="ISO 8601 last update")
    gallery_url: str | None = Field(default=None, description="Gallery page URL")
    download_count: int | None = Field(default=None, description="Total downloads, if known")
    dependencies: list[DependencyInfo] = Field(default_factory=list)
    installers: list[InstallerInfo] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list, description="Known versions (newest first)")
    raw_data: dict[str, Any] | None = Field(
        default=None,
        description="Select raw source fields preserved for reference (optional)",
    )
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cache_hit: bool = Field(default=False)

    model_config = {"extra": "ignore"}

    @property
    def latest(self) -> str:
        return self.version


class SearchResults(BaseModel):
    """Response from search_packages."""

    query: str
    total: int
    sources: list[SourceName]
    packages: list[PackageMetadata]


class CompareResult(BaseModel):
    """Side-by-side comparison of a package across sources."""

    package_id: str
    sources: dict[str, PackageMetadata | None] = Field(
        default_factory=dict,
        description="Mapping of source name to PackageMetadata (or None if absent)",
    )
    notes: list[str] = Field(default_factory=list)


class SwitchesResult(BaseModel):
    """Silent install/uninstall switches for a package."""

    package_id: str
    source: SourceName
    version: str | None = None
    silent_install_switch: str | None = Field(default=None)
    silent_with_progress_switch: str | None = Field(default=None)
    silent_uninstall_switch: str | None = Field(default=None)
    fallback_used: bool = Field(
        default=False,
        description="true when switches came from silentinstallhq, not the winget manifest",
    )
    installers: list[InstallerInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class InstallerMetadata(BaseModel):
    """Deep dive: all installers, hashes, archs, switches for a package/version."""

    package_id: str
    source: SourceName
    version: str
    release_notes_url: str | None = Field(default=None)
    release_notes: str | None = Field(default=None)
    upgrade_behavior: str | None = Field(default=None)
    installers: list[InstallerInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RecentResult(BaseModel):
    """Recently updated packages across sources."""

    limit: int
    source: str | None
    items: list[PackageMetadata]
