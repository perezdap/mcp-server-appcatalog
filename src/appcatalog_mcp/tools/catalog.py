"""Catalog tool — central registration of all MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from appcatalog_mcp.adapters import (
    ChocolateyAdapter,
    EvergreenAdapter,
    PackageNotFoundError,
    SihqAdapter,
    WingetAdapter,
)
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import (
    CompareResult,
    FindBestSourceResult,
    HashVerification,
    InstallerMetadata,
    PackageMetadata,
    RecentResult,
    SearchResults,
    SwitchesResult,
)

logger = logging.getLogger(__name__)

VALID_SOURCES = ("winget", "chocolatey", "silentinstallhq", "evergreen")


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


def _evergreen(ctx: Context) -> EvergreenAdapter:
    return ctx.request_context.lifespan_context["evergreen"]


def _adapter_for(ctx: Context, source: str):
    source = source.lower()
    if source == "winget":
        return _winget(ctx)
    if source == "chocolatey":
        return _choco(ctx)
    if source == "evergreen":
        return _evergreen(ctx)
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
        if s not in out:
            out.append(s)
    return out


# Candidate id forms to try against Chocolatey/Evergreen when the caller passes
# a winget-style ``Publisher.Package`` id. Chocolatey uses lowercased ids like
# ``googlechrome`` / ``7zip`` while winget uses ``Google.Chrome`` /
# ``7zip.7zip``. Evergreen uses PascalCase names like ``MicrosoftEdge`` / ``7zip``.
def _cross_source_id_candidates(package_id: str) -> list[str]:
    """Return distinct id spellings to try against Chocolatey/Evergreen.

    Order matters: try the most-likely form first so we avoid extra API calls.
    """
    candidates = [package_id]
    if "." in package_id:
        segments = package_id.split(".")
        # full id lowercased and dot-stripped (choco ``googlechrome`` / evergreen ``googlechrome``)
        joined = package_id.lower().replace(".", "")
        candidates.append(joined)
        # last segment lowercased (``7zip.7zip`` → ``7zip``)
        candidates.append(segments[-1].lower())
        # Title-cased last segment for Evergreen PascalCase names
        candidates.append(segments[-1][:1].upper() + segments[-1][1:])
        # ``Microsoft.VisualStudio.2022.Community`` → title-case of last-2 segs
        if len(segments) >= 2:
            joined_last_two = "".join(segments[-2:])
            candidates.append(joined_last_two.lower())
            candidates.append(joined_last_two[:1].upper() + joined_last_two[1:])
    # Deduplicate preserving order and casing. EVERGREEN endpoints are
    # case-sensitive (``/app/MicrosoftEdge`` is not ``/app/microsoftedge``),
    # so we must NOT collapse ``Chrome`` and ``chrome`` — they may route to
    # different API responses.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _score_package(pkg: PackageMetadata) -> tuple[int, list[str]]:
    """Return a heuristic packaging-score for a normalized PackageMetadata.

    Higher = better for a packaging agent. Encodes what's actually useful when
    building Intune/PSADT packages: real installer URLs with verifiable SHA256,
    MSI product/upgrade codes for detection, silent switches, and release notes.
    """
    score = 0
    reasons: list[str] = []
    installers = pkg.installers or []

    if installers:
        score += 3
        reasons.append(f"{len(installers)} installer(s)")
    installer_bonus = min(len(installers), 5)
    score += installer_bonus

    has_sha = [i for i in installers if i.sha256]
    if has_sha:
        score += 3
        score += min(len(has_sha), 6) // 2  # +1 per 2 hashed installers, cap 3
        reasons.append(f"{len(has_sha)} installer(s) with SHA256")

    has_silent = [i for i in installers if i.silent_switch]
    if has_silent:
        score += 2
        reasons.append("silent install switch present")

    has_product_code = [i for i in installers if i.product_code]
    if has_product_code:
        score += 3
        reasons.append(f"{len(has_product_code)} MSI product code(s) for detection")

    has_upgrade_code = [i for i in installers if i.upgrade_code]
    if has_upgrade_code:
        score += 1
        reasons.append("MSI upgrade code(s) for supersedence")

    if pkg.homepage:
        score += 1
        reasons.append("homepage known")

    if pkg.release_notes_url:
        score += 1
        reasons.append("release notes URL")

    if pkg.license:
        score += 1
        reasons.append("license known")

    # Freshness bonus: Evergreen fetches live from the vendor.
    if pkg.source == "evergreen":
        score += 2
        reasons.append("vendor-direct freshness (Evergreen)")

    # winget gets a small tie-break advantage for windows-desktop packaging
    # because its manifests also surface UpgradeBehavior and direct MSI URLs.
    if pkg.source == "winget" and installers:
        score += 1
        reasons.append("winget manifests preferred for Intune packaging")

    return score, reasons


def _hash_matches(expected_sha256: str, computed_sha256: str | None) -> bool:
    """Case-insensitive SHA256 equality used by ``verify_hash``.

    Returns False when nothing was computed (fetch failed / size cap hit) so a
    failed download can never be reported as a match.
    """
    if not computed_sha256:
        return False
    return computed_sha256.strip().lower() == expected_sha256.strip().lower()


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
    async def find_best_source(ctx: Context, package_id: str) -> FindBestSourceResult:
        """Recommend the single best source for packaging an application.

        Tries winget, Chocolatey, and Evergreen in parallel. For Chocolatey
        and Evergreen, also tries common id-translation spellings (e.g. winget
        ``Google.Chrome`` → choco ``googlechrome``; ``7zip.7zip`` → ``7zip``).

        Ranks each successfully-resolved package on:
        - presence of installer URLs with SHA256 hashes (+3 and extras)
        - silent install switches on any installer (+2)
        - MSI product/upgrade codes (+3 / +1, valuable for Intune detection)
        - homepage / license / release notes URL (small bonuses)
        - a small Evergreen freshness bonus (vendor-direct data) and a small
          winget preference for Win32 Intune packaging workflow.

        Returns the highest-scoring source plus the per-source score breakdown
        and reasons. Use this to choose between sources before calling
        ``get_installer_metadata`` or ``generate_psadt_wrapper``.

        Args:
            package_id: ``Publisher.Package`` (winget) works best; the same id
                is also tried against Chocolatey/Evergreen via spelling fallback.
        """
        import asyncio

        from appcatalog_mcp.models import CandidateScore

        async def _try_winget() -> CandidateScore:
            try:
                pkg = await _winget(ctx).get_package(package_id)
                score, reasons = _score_package(pkg)
                return CandidateScore(
                    source="winget", package=pkg, score=score, reasons=reasons
                )
            except (PackageNotFoundError, HttpClientError) as exc:
                return CandidateScore(source="winget", error=str(exc))

        async def _try_choco() -> CandidateScore:
            adapter = _choco(ctx)
            last_err: str | None = None
            for candidate in _cross_source_id_candidates(package_id):
                try:
                    pkg = await adapter.get_installer_detail(candidate)
                except (PackageNotFoundError, HttpClientError) as exc:
                    last_err = str(exc)
                    continue
                score, reasons = _score_package(pkg)
                reasons.append(f"chocolatey id matched as {candidate!r}")
                return CandidateScore(
                    source="chocolatey", package=pkg, score=score, reasons=reasons
                )
            return CandidateScore(source="chocolatey", error=last_err or "not found")

        async def _try_evergreen() -> CandidateScore:
            adapter = _evergreen(ctx)
            last_err: str | None = None
            for candidate in _cross_source_id_candidates(package_id):
                try:
                    pkg = await adapter.get_package(candidate)
                except (PackageNotFoundError, HttpClientError) as exc:
                    last_err = str(exc)
                    continue
                score, reasons = _score_package(pkg)
                reasons.append(f"evergreen app matched as {candidate!r}")
                return CandidateScore(
                    source="evergreen", package=pkg, score=score, reasons=reasons
                )
            return CandidateScore(source="evergreen", error=last_err or "not found")

        winget_c, choco_c, evergreen_c = await asyncio.gather(
            _try_winget(), _try_choco(), _try_evergreen()
        )
        candidates = [winget_c, choco_c, evergreen_c]
        best = max(candidates, key=lambda c: c.score, default=None)
        notes: list[str] = []
        if best and best.package is not None:
            notes.append(
                f"Best source: {best.source} (score {best.score}) — "
                + "; ".join(best.reasons)
            )
        else:
            notes.append("No source resolved this package id.")
        for c in candidates:
            if c.package is None and c.error:
                notes.append(f"{c.source}: unavailable ({c.error})")

        return FindBestSourceResult(
            package_id=package_id,
            best_source=best.source if best and best.package is not None else None,
            best_package=best.package if best and best.package is not None else None,
            best_score=best.score if best else 0,
            candidates=candidates,
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

    # -------------------------------------------------------------------------
    @mcp.tool()
    async def verify_hash(
        ctx: Context,
        url: str,
        expected_sha256: str,
        max_bytes: int | None = None,
    ) -> HashVerification:
        """Stream a download URL and confirm its SHA256 matches an expected value.

        Use this to prove that an installer URL actually returns the bytes whose
        hash a manifest claims, before trusting it for packaging. The download is
        streamed and hashed incrementally — it is never written to disk and never
        cached, so every call hits the live URL.

        The download is capped at ``max_bytes`` (default from
        ``APPCATALOG_VERIFY_MAX_BYTES``, 500 MB) to avoid accidentally pulling a
        multi-GB file; if the cap is hit, ``match`` is False and ``error`` is
        ``"size_limit_exceeded"``. Only ``http``/``https`` URLs are fetched;
        other schemes return ``match=False`` with an ``unsupported_url_scheme``
        error.

        Args:
            url: direct installer download URL to fetch.
            expected_sha256: the SHA256 hex digest you expect (case-insensitive).
            max_bytes: optional override of the per-call download cap in bytes.

        Examples:
            verify_hash(url="https://.../app.msi", expected_sha256="AB12...")
        """
        import time

        settings = _settings(ctx)
        cap = max_bytes if max_bytes and max_bytes > 0 else settings.verify_max_bytes
        expected_norm = expected_sha256.strip().lower()

        # Only fetch http(s). verify_hash is the one tool that takes a fully
        # caller-controlled URL, so reject other schemes (file://, ftp://, etc.)
        # to avoid turning the server into an arbitrary-scheme fetcher.
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme not in ("http", "https"):
            return HashVerification(
                url=url,
                expected_sha256=expected_norm,
                computed_sha256=None,
                match=False,
                error="unsupported_url_scheme (only http/https allowed)",
            )

        start = time.monotonic()
        digest, bytes_read, status, error = await _http(ctx).stream_sha256(
            url,
            max_bytes=cap,
            block_private_hosts=settings.verify_block_private_hosts,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return HashVerification(
            url=url,
            expected_sha256=expected_norm,
            computed_sha256=digest,
            match=_hash_matches(expected_norm, digest),
            bytes_read=bytes_read,
            elapsed_ms=elapsed_ms,
            status_code=status,
            error=error,
        )
