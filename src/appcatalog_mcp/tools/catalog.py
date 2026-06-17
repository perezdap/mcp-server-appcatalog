"""Catalog tool — central registration of all MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from appcatalog_mcp.adapters import (
    ChocolateyAdapter,
    PackageNotFoundError,
    SihqAdapter,
    WingetAdapter,
)
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import (
    CompareResult,
    InstallerMetadata,
    PackageMetadata,
    RecentResult,
    SearchResults,
    SwitchesResult,
)

logger = logging.getLogger(__name__)

VALID_SOURCES = ("winget", "chocolatey", "silentinstallhq")


def _http(ctx: Context) -> HttpClient:
    return ctx.request_context.lifespan_context["http"]


def _settings(ctx: Context) -> Settings:
    return ctx.request_context.lifespan_context["settings"]


def _sihq(ctx: Context) -> SihqAdapter:
    return ctx.request_context.lifespan_context["sihq"]


def _winget(ctx: Context) -> WingetAdapter:
    return ctx.request_context.lifespan_context["winget"]


def _choco(ctx: Context) -> ChocolateyAdapter:
    return ctx.request_context.lifespan_context["chocolatey"]


def _adapter_for(ctx: Context, source: str):
    source = source.lower()
    if source == "winget":
        return _winget(ctx)
    if source == "chocolatey":
        return _choco(ctx)
    if source == "silentinstallhq":
        return _sihq(ctx)
    raise ValueError(f"Unknown source {source!r}. Valid: {VALID_SOURCES}")


def _normalize_sources(sources: list[str] | None) -> list[str]:
    if not sources:
        return ["winget", "chocolatey"]
    out: list[str] = []
    for s in sources:
        s = s.strip().lower()
        if s not in VALID_SOURCES:
            raise ValueError(f"Unknown source {s!r}. Valid: {VALID_SOURCES}")
        if s not in (out):
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Public tool registrations
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP) -> None:
    """Register all appcatalog MCP tools."""

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def search_packages(
        ctx: Context,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ) -> SearchResults:
        """Search winget and/or Chocolatey for application packages.

        Returns a normalized list of packages with id, name, publisher, latest
        version, description, and (when available) installer URLs and SHA256
        hashes. Searches run in parallel across the selected sources.

        Args:
            query: keyword or application name (e.g. "Google Chrome", "7-Zip").
            sources: list of sources to query. Defaults to winget + chocolatey.
            limit: max results per source (cap 50).

        Examples:
            search_packages(query="Chrome")
            search_packages(query="vlc", sources=["winget"], limit=5)
        """
        import asyncio

        limit = max(1, min(limit, 50))
        selected = _normalize_sources(sources)
        # SIHQ isn't searchable as a package catalog; skip it in search.
        searchable = [s for s in selected if s != "silentinstallhq"]

        async def _one(source: str) -> list[PackageMetadata]:
            try:
                adapter = _adapter_for(ctx, source)
                return await adapter.search(query, limit=limit)
            except (HttpClientError, PackageNotFoundError) as exc:
                logger.warning("search_packages(%s) failed: %s", source, exc)
                return []

        gathered = await asyncio.gather(*[_one(s) for s in searchable])
        packages: list[PackageMetadata] = []
        for batch in gathered:
            packages.extend(batch)
        # Interleave (winget first, chocolatey second) — already in order due to gather.
        return SearchResults(
            query=query,
            total=len(packages),
            sources=searchable,  # type: ignore[arg-type]
            packages=packages[: limit * len(searchable)],
        )

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def get_package(
        ctx: Context,
        package_id: str,
        source: str | None = None,
        version: str | None = None,
    ) -> PackageMetadata:
        """Get full metadata for a specific package, latest version by default.

        If ``source`` is None, tries winget first then Chocolatey. If a ``source``
        is given, only that source is queried. Returns normalized metadata with
        installers, hashes, tags, dependencies, and gallery links.

        Args:
            package_id: ``Publisher.Package`` (winget) or package id (chocolatey).
            source: "winget", "chocolatey", or None (auto: winget→chocolatey).
            version: specific version; None means latest.
        """
        if source:
            return await _adapter_for(ctx, source).get_package(package_id, version=version)

        # Auto try winget first, then chocolatey.
        last_exc: Exception | None = None
        for src in ("winget", "chocolatey"):
            try:
                return await _adapter_for(ctx, src).get_package(package_id, version=version)
            except PackageNotFoundError as exc:
                last_exc = exc
                continue
            except HttpClientError as exc:
                logger.warning("get_package(%s) transient failure: %s", src, exc)
                last_exc = exc
                continue
        raise PackageNotFoundError(
            f"Package {package_id!r} not found in winget or chocolatey"
        ) from last_exc

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def get_installer_metadata(
        ctx: Context,
        package_id: str,
        source: str = "winget",
        version: str | None = None,
    ) -> InstallerMetadata:
        """Deep dive: all installer URLs, SHA256 hashes, architectures,
        installer types, scopes, product codes, upgrade codes, and silent
        switches for one package version.

        Defaults to winget, which exposes the most detailed installer metadata
        including per-installer ProductCode and UpgradeCode. For Chocolatey,
        returns the .nupkg download URL plus note (its .nupkg is a wrapper, not
        the raw installer).

        Args:
            package_id: ``Publisher.Package`` (winget) or package id (chocolatey).
            source: "winget" (default) or "chocolatey".
            version: specific version; None means latest.
        """
        adapter = _adapter_for(ctx, source)
        pkg = await adapter.get_installer_detail(package_id, version=version)
        raw = pkg.raw_data or {}
        notes: list[str] = []
        if source == "chocolatey":
            notes.append(
                "Chocolatey download URL is the .nupkg zip (chocolateyInstall.ps1 + tools), "
                "not the raw installer. Inspect tools/chocolateyInstall.ps1 for the actual "
                "silent switches inside."
            )
        return InstallerMetadata(
            package_id=pkg.id,
            source=source,  # type: ignore[arg-type]
            version=pkg.version,
            release_notes_url=pkg.release_notes_url,
            release_notes=pkg.release_notes,
            upgrade_behavior=raw.get("upgrade_behavior"),
            installers=pkg.installers,
            notes=notes,
        )

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def compare_sources(ctx: Context, package_id: str) -> CompareResult:
        """Side-by-side comparison: what winget vs Chocolatey have for the app.

        Useful for choosing the better source for packaging (e.g. winget's direct
        MSI URL + ProductCode vs Chocolatey's community-maintained nupkg wrapper).

        Args:
            package_id: identifier as queried in the source. For best results use
                the winget ``Publisher.Package`` form; the same id is also tried
                as-is against Chocolatey (which uses lowercase ids like ``7zip``).
        """
        import asyncio

        async def _try(source: str) -> PackageMetadata | None:
            try:
                return await _adapter_for(ctx, source).get_package(package_id)
            except (PackageNotFoundError, HttpClientError) as exc:
                logger.info("compare_sources(%s): %s unavailable (%s)", source, package_id, exc)
                return None

        winget_pkg, choco_pkg = await asyncio.gather(_try("winget"), _try("chocolatey"))

        notes: list[str] = []
        if winget_pkg and choco_pkg:
            notes.append(
                f"winget v{winget_pkg.version} ({winget_pkg.publisher}) vs "
                f"chocolatey v{choco_pkg.version} ({choco_pkg.publisher})"
            )
            notes.append(
                "winget provides direct installer URLs + ProductCode; Chocolatey "
                "provides a community-maintained .nupkg wrapper."
            )
        elif winget_pkg:
            notes.append("Only winget has this package.")
        elif choco_pkg:
            notes.append("Only Chocolatey has this package.")
        else:
            notes.append("Package not found in either source (try lowercase id for Chocolatey).")

        return CompareResult(
            package_id=package_id,
            sources={
                "winget": winget_pkg,
                "chocolatey": choco_pkg,
            },
            notes=notes,
        )

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def list_recent(
        ctx: Context,
        limit: int = 10,
        source: str | None = None,
    ) -> RecentResult:
        """List recently updated packages across sources.

        Args:
            limit: max packages per source (cap 50).
            source: restrict to one of "winget", "chocolatey". None → both.
        """
        import asyncio

        limit = max(1, min(limit, 50))
        if source:
            sources = [source.lower()]
        else:
            sources = ["winget", "chocolatey"]

        async def _one(src: str) -> list[PackageMetadata]:
            try:
                return await _adapter_for(ctx, src).list_recent(limit=limit)
            except (HttpClientError, PackageNotFoundError) as exc:
                logger.warning("list_recent(%s) failed: %s", src, exc)
                return []

        batches = await asyncio.gather(*[_one(s) for s in sources])
        items: list[PackageMetadata] = []
        for batch in batches:
            items.extend(batch)
        return RecentResult(limit=limit, source=source, items=items[: limit * len(sources)])

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def get_silent_switches(
        ctx: Context,
        package_id: str,
        source: str = "winget",
    ) -> SwitchesResult:
        """Extract silent install/uninstall switches for a package.

        Prefers per-installer switches from the winget manifest (most accurate,
        per-architecture). If the manifest carries no Silent/SilentWithProgress
        switch, falls back to the standalone Silent Install HQ MCP server's
        ``extract_switches`` guide (when configured).

        Args:
            package_id: ``Publisher.Package`` (winget) or chocolatey id.
            source: "winget" (default) — switches come from the installer manifest.
        """
        adapter = _adapter_for(ctx, source)
        pkg = await adapter.get_package(package_id)

        silent = None
        silent_with_progress = None
        for inst in pkg.installers:
            silent = inst.silent_switch or silent
            silent_with_progress = inst.silent_with_progress_switch or silent_with_progress
            if silent and silent_with_progress:
                break

        fallback_used = False
        notes: list[str] = []
        if not silent and source == "winget":
            # Try the SIHQ fallback. SIHQ knows software by display name.
            query_name = pkg.name or package_id
            switches_data = await _sihq(ctx)._call_extract_switches(query_name)
            if switches_data:
                silent = switches_data.get("silent_install_switch") or silent
                silent_with_progress = (
                    switches_data.get("silent_with_progress_switch") or silent_with_progress
                )
                fallback_used = True
                notes.append(f"Fetched switches from Silent Install HQ for {query_name!r}.")
            else:
                notes.append("No manifest switches and Silent Install HQ fallback unavailable.")

        if silent:
            notes.append("Switches are installer-type-specific; verify against the executable.")
        return SwitchesResult(
            package_id=pkg.id,
            source=source,  # type: ignore[arg-type]
            version=pkg.version,
            silent_install_switch=silent,
            silent_with_progress_switch=silent_with_progress,
            installers=pkg.installers,
            fallback_used=fallback_used,
            notes=notes,
        )

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def get_changelog_or_releasenotes(
        ctx: Context,
        package_id: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Return release notes URL (and field text if present) for a package.

        winget manifests carry ``ReleaseNotesUrl`` and ``ReleaseNotes`` fields in
        the locale manifest. Chocolatey exposes ``ReleaseNotes`` (often a URL).

        Args:
            package_id: ``Publisher.Package`` (winget) or chocolatey id.
            source: "winget" or "chocolatey". None → try winget then chocolatey.
        """
        if source:
            pkg = await _adapter_for(ctx, source).get_package(package_id)
        else:
            pkg = await get_package(ctx, package_id=package_id)  # type: ignore[misc]
        return {
            "package_id": pkg.id,
            "source": pkg.source,
            "version": pkg.version,
            "release_notes_url": pkg.release_notes_url,
            "release_notes": pkg.release_notes,
        }
