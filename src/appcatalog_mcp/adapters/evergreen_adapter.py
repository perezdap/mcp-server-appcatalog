"""Evergreen adapter.

Source: the public Evergreen REST API maintained by Aaron Parker / EUCPilots.
Base URL: ``https://evergreen-api.stealthpuppy.com``

Unlike the manifest repos (winget, Chocolatey) Evergreen queries the *vendor*
source for the latest version + download URL + (when available) SHA256. It
covers 200+ common enterprise applications (Microsoft Edge, 7-Zip, Adobe
Reader, Zoom, Cisco Webex, FSLogix, …). Data refreshes every 8 hours.

Endpoints:
- ``GET /apps``                   → list of supported apps ``[{"Name": "7zip"}, ...]``
- ``GET /app/{Name}``            → list of installer rows for one app::

    [{
      "Version": "26.01",
      "Date": "27/4/2026",                # DD/MM/YYYY
      "Size": 1663430,
      "Sha256": "cdea...",                 # lowercase hex SHA256, when vendor exposes one
      "Architecture": "ARM",               # ARM, ARM64, x64, x86, x86_64, neutral…
      "InstallerType": "Default",          # evergreen's internal label
      "Type": "exe",                       # canonical installer type (exe, msi, msix…)
      "URI": "https://...",
      "Channel": "Stable",                 # optional
      "Release": "Enterprise",             # optional
    }, ...]

Quirks:
- The API blocks default User-Agents; our configured UA is fine.
- ``/apps`` is the only way to discover what's supported (no free-text search
  server-side). We fetch it once per TTL and fuzzy-match client-side.
- No "recent updates" endpoint; ``list_recent`` returns ``[]``.
- Dates are DD/MM/YYYY (EU). We sort by parsed Date to pick the latest version.
- One ``/app/{Name}`` response can span multiple Version/Channel/Release
  combinations; "latest" = the entry with the newest ``Date`` (or, failing
  that, the lexicographically highest ``Version``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from appcatalog_mcp.adapters.base import PackageAdapter, PackageNotFoundError
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import InstallerInfo, PackageMetadata

logger = logging.getLogger(__name__)


def _parse_evergreen_date(date_str: str | None) -> datetime | None:
    """Evergreen dates are DD/MM/YYYY; tolerate ISO and dash formats."""
    if not date_str:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _latest_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the rows belonging to the *latest* version in an /app/{name} response."""
    if not entries:
        return []

    def sort_key(row: dict[str, Any]) -> tuple[int, str]:
        # Most recent Date wins; tie-break on Version string so we don't pick
        # a Beta/Dev channel row just because it sorts after a Stable one.
        date_obj = _parse_evergreen_date(row.get("Date"))
        date_rank = int(date_obj.timestamp()) if date_obj else 0
        return (date_rank, str(row.get("Version") or ""))

    rows = list(entries)
    best = max(rows, key=sort_key)
    best_version = str(best.get("Version") or "")
    # Return every row of that latest version — multi-arch installers share it.
    return [r for r in rows if str(r.get("Version") or "") == best_version]


def _normalize_arch(arch: str | None) -> str:
    if not arch:
        return "neutral"
    a = arch.strip()
    # Canonicalise the common variants.
    canonical = {
        "x86_64": "x64",
        "amd64": "x64",
        "win64": "x64",
        "win32": "x86",
        "i386": "x86",
        "any": "neutral",
        "": "neutral",
    }
    return canonical.get(a.lower(), a.lower())


class EvergreenAdapter(PackageAdapter):
    """Adapter for the public Evergreen REST API."""

    name = "evergreen"
    display_label = "Evergreen"

    def __init__(self, http: HttpClient, api_base: str) -> None:
        super().__init__(http)
        self.api_base = api_base.rstrip("/")

    # ---- Public API --------------------------------------------------------
    async def search(self, query: str, *, limit: int = 10) -> list[PackageMetadata]:
        limit = max(1, min(limit, 50))
        cache_key = self.cache_key(f"search:{query.lower().strip()}:{limit}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return [PackageMetadata.model_validate(p) for p in cached]

        supported = await self._list_apps()
        query_lower = query.strip().lower()
        # Substring match on Name; Evergreen names are sometimes PascalCase
        # (``MicrosoftEdge``) so also split into individual tokens to catch
        # ``Microsoft Edge`` → ``MicrosoftEdge`` and ``edge`` → ``MicrosoftEdge``.
        matches: list[str] = []
        for app in supported:
            name = (app.get("Name") or "").strip()
            if not name:
                continue
            n_lower = name.lower()
            # Treat user spaces as wildcard; ``microsoft edge`` matches ``MicrosoftEdge``.
            compact = n_lower.replace(" ", "")
            query_compact = query_lower.replace(" ", "")
            if query_compact and (
                query_compact in compact or query_lower in n_lower
            ):
                matches.append(name)
            if len(matches) >= limit * 2:
                break

        results: list[PackageMetadata] = []
        for name in matches[:limit]:
            try:
                results.append(await self.get_package(name))
            except (PackageNotFoundError, HttpClientError) as exc:
                logger.debug("evergreen get_package(%s) failed during search: %s", name, exc)
            if len(results) >= limit:
                break

        self.http.set_cache(cache_key, [p.model_dump(mode="json") for p in results])
        return results

    async def get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        cache_key = self.cache_key(
            f"pkg:{package_id.lower()}:{version or 'latest'}"
        )
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            meta = PackageMetadata.model_validate(cached)
            meta.cache_hit = True
            return meta

        rows = await self._fetch_app_rows(package_id)
        if not rows:
            raise PackageNotFoundError(
                f"Evergreen application {package_id!r} not found"
            )

        desired = _latest_rows(rows)
        if version is not None:
            desired = [r for r in rows if str(r.get("Version")) == version]
            if not desired:
                raise PackageNotFoundError(
                    f"Evergreen {package_id!r} has no version {version!r}"
                )

        meta = self.normalize(
            {"name": package_id, "version": version, "rows": desired, "all_rows": rows}
        )
        self.http.set_cache(cache_key, meta.model_dump(mode="json"))
        return meta

    async def get_installer_detail(
        self, package_id: str, version: str | None = None
    ) -> PackageMetadata:
        return await self.get_package(package_id, version=version)

    async def list_recent(self, *, limit: int = 10) -> list[PackageMetadata]:
        # Evergreen doesn't expose updates-by-date; ``/apps`` is alphabetical.
        # Return [] so the cross-source ``list_recent`` tool just skips us.
        return []

    # ---- Fetch helpers -----------------------------------------------------
    async def _list_apps(self) -> list[dict[str, Any]]:
        cache_key = self.cache_key("apps")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return cached
        url = f"{self.api_base}/apps"
        try:
            data, _ = await self.http.fetch_json(
                url, cache_key=cache_key, use_cache=True
            )
        except HttpClientError as exc:
            logger.warning("Evergreen /apps failed: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        # Cache explicitly so callers using a mocked fetch_json still benefit
        # from the caching layer.
        self.http.set_cache(cache_key, data)
        return data
    async def _fetch_app_rows(self, name: str) -> list[dict[str, Any]]:
        # URL is case-sensitive in practice — pass the exact Name from /apps.
        # When a caller passes a lowercase id we try the original casing first
        # and then fall back to a TitleCased guess.
        candidates: list[str] = []
        if name:
            # 1) Exact id (preserves caller casing)
            candidates.append(name)
            # 2) First-letter Title-Cased variant (e.g. ``microsoftedge`` → ``Microsoftedge``)
            candidates.append(name[:1].upper() + name[1:])
            # 3) Lookup the canonical casing from /apps
        seen: set[str] = set()
        for candidate in candidates:
            cl = candidate.lower()
            if cl in seen:
                continue
            seen.add(cl)
            # Even if the caller gave us a lowercase name, /apps gives us the
            # canonical PascalCase spelling, so look it up to be safe.
            canonical = await self._resolve_canonical_name(cl)
            if canonical and canonical.lower() != cl:
                candidates.append(canonical)

        tried: list[str] = []
        for candidate in candidates:
            if candidate in tried:
                continue
            tried.append(candidate)
            url = f"{self.api_base}/app/{candidate}"
            try:
                data, _ = await self.http.fetch_json(url)
            except HttpClientError as exc:
                logger.debug("Evergreen /app/%s failed: %s", candidate, exc)
                continue
            if isinstance(data, list) and data:
                return data
        return []

    async def _resolve_canonical_name(self, lower_name: str) -> str | None:
        apps = await self._list_apps()
        for app in apps:
            n = app.get("Name")
            if n and n.lower() == lower_name:
                return n
        return None

    # ---- Normalization -----------------------------------------------------
    @staticmethod
    def normalize(raw: Any) -> PackageMetadata:
        if not isinstance(raw, dict):
            raise TypeError("EvergreenAdapter.normalize expects a dict")
        name: str = raw["name"]
        rows: list[dict[str, Any]] = list(raw.get("rows") or [])
        all_rows: list[dict[str, Any]] = list(raw.get("all_rows") or rows)
        # The chosen "latest" version = the Version of every row in `rows`.
        if rows:
            version = str(rows[0].get("Version") or "")
        else:
            version = str(raw.get("version") or "") or "unknown"

        installers: list[InstallerInfo] = []
        channels: set[str] = set()
        releases: set[str] = set()
        latest_date: str | None = None
        for row in rows:
            sha = (row.get("Sha256") or row.get("Hash"))
            if isinstance(sha, str):
                sha = sha.lower() or None
            uri = row.get("URI") or row.get("Url")
            installer_type = (row.get("Type") or row.get("InstallerType") or "unknown")
            arch = _normalize_arch(row.get("Architecture"))
            file_size = row.get("Size")
            if isinstance(file_size, str) and file_size.isdigit():
                file_size = int(file_size)
            elif not isinstance(file_size, int):
                file_size = None
            ch = row.get("Channel")
            rel = row.get("Release")
            if ch:
                channels.add(str(ch))
            if rel:
                releases.add(str(rel))
            row_date = row.get("Date")
            if row_date and not latest_date:
                latest_date = str(row_date)
            installers.append(
                InstallerInfo(
                    url=uri or "",
                    sha256=sha if sha else None,
                    installer_type=str(installer_type).lower(),
                    architecture=arch,
                    scope=None,
                    product_code=None,
                    upgrade_code=None,
                    silent_switch=None,
                    file_size=file_size,
                )
            )

        versions: list[str] = []
        for r in all_rows:
            v = str(r.get("Version") or "")
            if v and v not in versions:
                versions.append(v)
        # Order newest-first by Date (fall back to string ordering).
        versions.sort(
            key=lambda v: (
                _parse_evergreen_date(
                    next(
                        (r.get("Date") for r in all_rows if str(r.get("Version")) == v),
                        None,
                    )
                ) or datetime.min,
                v,
            ),
            reverse=True,
        )

        raw_data: dict[str, Any] = {
            "channels": sorted(channels),
            "releases": sorted(releases),
            "latest_date": latest_date,
            "vendor_source": True,
            "note": (
                "Evergreen returns live latest-version data fetched directly "
                "from the vendor (not a manifest repo). Can be newer than "
                "winget/Chocolatey for some apps. Does not carry publisher/"
                "homepage/license metadata."
            ),
        }

        return PackageMetadata(
            source="evergreen",
            id=name,
            name=name,
            publisher="",
            version=version,
            description="",
            homepage=None,
            license=None,
            license_url=None,
            release_notes_url=None,
            release_notes=None,
            tags=[],
            moniker=None,
            update_date=latest_date,
            gallery_url="https://stealthpuppy.com/apptracker/",
            download_count=None,
            dependencies=[],
            installers=installers,
            versions=versions,
            raw_data=raw_data,
        )
