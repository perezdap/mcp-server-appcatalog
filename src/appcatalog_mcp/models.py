"""Pydantic models for normalized package metadata and MCP tool responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceName = Literal["winget", "chocolatey", "silentinstallhq", "evergreen"]

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


class CandidateScore(BaseModel):
    """One source's score in a find_best_source comparison."""

    source: SourceName
    package: PackageMetadata | None = None
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    error: str | None = None


class FindBestSourceResult(BaseModel):
    """Recommendation of the best single source for packaging a given app.

    Ranking rewards the presence of installer URLs with SHA256 hashes, silent
    switches, MSI product/upgrade codes (valuable for Intune detection),
    release notes, and (slightly) Evergreen's vendor-direct freshness.
    """

    package_id: str
    best_source: SourceName | None = None
    best_package: PackageMetadata | None = None
    best_score: int = 0
    candidates: list[CandidateScore] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class HashVerification(BaseModel):
    """Result of streaming a download URL and computing its SHA256.

    Used by ``verify_hash`` to confirm that an installer URL actually returns
    the bytes whose hash a manifest claims. The download is streamed and hashed
    incrementally (never written to disk, never cached) and capped at
    ``max_bytes`` to avoid pulling multi-GB files by accident.
    """

    url: str
    expected_sha256: str
    computed_sha256: str | None = Field(
        default=None, description="SHA256 of the bytes read (None on fetch failure)"
    )
    match: bool = Field(
        default=False, description="True only when the full download hash equals expected"
    )
    bytes_read: int = Field(default=0, description="Bytes streamed before completion or cap")
    elapsed_ms: int = Field(default=0)
    status_code: int = Field(default=0, description="HTTP status, 0 if the request never started")
    error: str | None = Field(
        default=None,
        description="'size_limit_exceeded' when capped, or the fetch error message",
    )
