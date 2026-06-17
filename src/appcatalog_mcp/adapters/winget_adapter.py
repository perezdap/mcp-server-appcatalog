"""winget adapter.

Primary source: GitHub Contents API on ``microsoft/winget-pkgs`` (authoritative,
current manifests, but rate-limited at 60 req/hr anonymous / 5000 with a token).

Fallback source: ``winget.run`` REST API (``api.winget.run``). No rate limit but
its data was last indexed in early 2023, so it's a degraded last resort for
search/recent only; the GitHub path is always preferred for full manifests.

Manifest layout on disk (winget-pkgs repo):

    manifests/{first_letter}/{Publisher}/{Package}/{Version}/
        {PackageIdentifier}.yaml                  # version manifest
        {PackageIdentifier}.installer.yaml        # installers
        {PackageIdentifier}.locale.default.yaml   # default locale
        {PackageIdentifier}.locale.en-US.yaml      # en-US locale
        {PackageIdentifier}.locale.<lang>.yaml     # other locales

Multi-channel packages (e.g. Google Chrome Beta) live under nested dirs like
``manifests/g/Google/Chrome/Beta/{Version}/``; this adapter surfaces only the
"root" channel (identifiable by PackageIdentifier == ``{Publisher}.{Package}``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from appcatalog_mcp.adapters.base import PackageAdapter, PackageNotFoundError
from appcatalog_mcp.config import Settings
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import (
    DependencyInfo,
    InstallerInfo,
    PackageMetadata,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
WINGET_REPO = "microsoft/winget-pkgs"
WINGET_RUN_API = "https://api.winget.run"

# Matches a "version-like" directory name. winget versions are dotted numbers
# like 1.2.3 or 149.0.7827.156, occasionally with prerelease/beta suffix.
VERSION_RE = re.compile(r"^\d+(\.\d+)+([\-._][A-Za-z0-9]+)*$")

# Directory names that are NOT versions (channels, nested packages, scopes).
_NON_VERSION_DIRS = {
    "EXE", "MSI", "MSIX", "PORTABLE", "ZIP", "APPX",
}


class WingetRunNotConfiguredError(Exception):
    """Internal: winget.run disabled by config."""


class WingetAdapter(PackageAdapter):
    """Adapter for winget manifests (GitHub primary, winget.run fallback)."""

    name = "winget"
    display_label = "winget"

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        super().__init__(http)
        self.settings = settings
        self.gh_headers = settings.github_auth_headers
        self.mode = settings.winget_api.lower()
        # Circuit breaker for winget.run: after a transient failure, we skip
        # retries for a cooldown so we don't hammer a degraded upstream, but
        # the breaker auto-resets so a long-running streamable-http server can
        # recover without restart. It is never tripped permanently.
        self._wingetrun_disabled_until: float = 0.0
        self._wingetrun_cooldown_seconds: float = 300.0  # 5 min cooldown

    # ---- Lookup helpers ----------------------------------------------------
    @staticmethod
    def split_package_id(package_id: str) -> tuple[str, str, str]:
        """Return (publisher, package_path, first_letter) for an id like
        ``Microsoft.VisualStudio.2022.Community``.

        winget-pkgs nests EVERY dot-segment of the PackageIdentifier as its own
        directory under ``manifests/{first_letter}/``. The first segment is the
        publisher; the remaining segments, joined by ``/``, form the package
        path on disk. Common ``Publisher.Package`` ids both surface as a
        two-level path (``Google`` / ``Chrome``).
        """
        if "." not in package_id:
            raise PackageNotFoundError(
                f"winget package ids are Publisher.Package form (got {package_id!r})"
            )
        segments = package_id.split(".")
        publisher = segments[0]
        package_path = "/".join(segments[1:])
        first_letter = publisher[0].lower()
        return publisher, package_path, first_letter

    def version_dir_path(self, package_id: str, version: str) -> str:
        publisher, package_path, first_letter = self.split_package_id(package_id)
        return f"manifests/{first_letter}/{publisher}/{package_path}/{version}"

    def package_dir_path(self, package_id: str) -> str:
        publisher, package_path, first_letter = self.split_package_id(package_id)
        return f"manifests/{first_letter}/{publisher}/{package_path}"

    @property
    def _wingetrun_disabled(self) -> bool:
        import time
        return time.time() < self._wingetrun_disabled_until

    def _trip_wingetrun_breaker(self) -> None:
        import time
        self._wingetrun_disabled_until = time.time() + self._wingetrun_cooldown_seconds

    # ---- Public API --------------------------------------------------------
    async def search(self, query: str, *, limit: int = 10) -> list[PackageMetadata]:
        limit = max(1, min(limit, 50))
        cache_key = self.cache_key(f"search:{query.lower().strip()}:{limit}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return [PackageMetadata.model_validate(p) for p in cached]

        # GitHub has no free-text search across winget-pkgs manifests at the
        # package-name level, so always go to winget.run for search. It returns
        # Package summaries; we layer real versions/manifests lazily via get_package.
        results: list[PackageMetadata] = []
        consulted_upstream = False
        if self.mode != "github" and not self._wingetrun_disabled:
            consulted_upstream = True
            try:
                results = await self._wingetrun_search(query, limit=limit)
            except (HttpClientError, WingetRunNotConfiguredError) as exc:
                logger.warning("winget.run search failed (%s); skipping cache", exc)
                # Trip the time-bounded breaker so subsequent searches skip the
                # degraded upstream, and DO NOT cache the empty failure result.
                self._trip_wingetrun_breaker()
                return results
        # Only persist results when we actually consulted the upstream (or it was
        # intentionally disabled via ``github`` mode, in which case the empty
        # result is the definitive answer for the TTL). Skipped-during-cooldown
        # queries never touch the cache so the breaker can reset and retry.
        if consulted_upstream:
            self.http.set_cache(
                cache_key, [p.model_dump(mode="json") for p in results]
            )
        return results

    async def get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        cache_key = self.cache_key(f"pkg:{package_id.lower()}:{version or 'latest'}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            meta = PackageMetadata.model_validate(cached)
            meta.cache_hit = True
            return meta

        meta = await self._github_get_package(package_id, version=version)
        self.http.set_cache(cache_key, meta.model_dump(mode="json"))
        return meta

    async def get_manifest(
        self, package_id: str, version: str | None = None
    ) -> dict[str, Any]:
        version = version or await self._github_latest_version(package_id)
        return await self._github_fetch_manifest_files(package_id, version)

    async def get_installer_detail(
        self, package_id: str, version: str | None = None
    ) -> PackageMetadata:
        return await self.get_package(package_id, version=version)

    async def list_recent(self, *, limit: int = 10) -> list[PackageMetadata]:
        # GitHub commits-by-path gives us recently updated manifest dirs.
        limit = max(1, min(limit, 50))
        cache_key = self.cache_key(f"recent:{limit}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return [PackageMetadata.model_validate(p) for p in cached]

        package_ids = await self._github_recent_package_ids(limit=limit)
        results: list[PackageMetadata] = []
        for pid in package_ids:
            try:
                results.append(await self.get_package(pid))
            except PackageNotFoundError:
                continue
            if len(results) >= limit:
                break
        self.http.set_cache(cache_key, [p.model_dump(mode="json") for p in results])
        return results

    # ---- GitHub Contents API ----------------------------------------------
    async def _github_latest_version(self, package_id: str) -> str:
        """List the version dir for a package, pick the highest semver."""
        path = self.package_dir_path(package_id)
        url = f"{GITHUB_API}/repos/{WINGET_REPO}/contents/{path}"
        try:
            data, _ = await self.http.fetch_json(
                url,
                headers=self.gh_headers,
                cache_key=f"gh:dir:{package_id.lower()}",
            )
        except HttpClientError as exc:
            raise PackageNotFoundError(f"winget package {package_id!r} not found: {exc}") from exc

        if not isinstance(data, list):
            raise PackageNotFoundError(f"winget package {package_id!r} has no version dir")

        version_dirs: list[str] = []
        for entry in data:
            name = entry.get("name", "")
            if entry.get("type") == "dir" and self._looks_like_version(name):
                version_dirs.append(name)

        if not version_dirs:
            raise PackageNotFoundError(f"no versions found for winget {package_id!r}")
        version_dirs.sort(key=_version_sort_key, reverse=True)
        return version_dirs[0]

    async def _github_get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        version = version or await self._github_latest_version(package_id)
        manifests = await self._github_fetch_manifest_files(package_id, version)
        return self.normalize(
            {"package_id": package_id, "version": version, "manifests": manifests}
        )

    async def _github_fetch_manifest_files(
        self, package_id: str, version: str
    ) -> dict[str, Any]:
        """Fetch the YAML manifest files for one package+version from raw URLs."""
        path = self.version_dir_path(package_id, version)
        base_url = f"{GITHUB_RAW}/{WINGET_REPO}/master/{path}"

        wanted = {
            "version": f"{package_id}.yaml",
            "installer": f"{package_id}.installer.yaml",
            "default_locale": f"{package_id}.locale.default.yaml",
            "locale_en_us": f"{package_id}.locale.en-US.yaml",
        }

        manifests: dict[str, Any] = {}
        for key, filename in wanted.items():
            url = f"{base_url}/{filename}"
            try:
                text, hit = await self.http.fetch_text(
                    url,
                    cache_key=f"gh:file:{package_id.lower()}:{version}:{key}",
                )
            except HttpClientError as exc:
                logger.debug("Manifest file %s not found: %s", filename, exc)
                continue
            if hit:
                logger.debug("Manifest cache hit: %s", key)
            try:
                manifests[key] = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                logger.warning("YAML parse error in %s: %s", filename, exc)
                manifests[key] = {}
        if "installer" not in manifests and "version" not in manifests:
            raise PackageNotFoundError(
                f"winget {package_id!r} version {version} could not be fetched"
            )
        return manifests

    async def _github_recent_package_ids(self, *, limit: int) -> list[str]:
        """Discover recently-updated package ids via the commits API.

        We walk recent commits to the ``manifests/`` directory and extract
        package identifiers from touched paths.
        """
        url = f"{GITHUB_API}/repos/{WINGET_REPO}/commits"
        params = {"path": "manifests", "per_page": min(limit * 3, 100)}
        seen: list[str] = []
        try:
            data, _ = await self.http.fetch_json(
                url,
                headers=self.gh_headers,
                params=params,
                cache_key=self.cache_key(f"commits:{limit}"),
            )
        except HttpClientError as exc:
            logger.warning("Failed to fetch recent commits: %s", exc)
            return []
        for commit in data if isinstance(data, list) else []:
            msg = (commit.get("commit", {}).get("message") or "")
            touched: list[str] = []
            files = commit.get("files") or []
            for f in files:
                fp = f.get("filename", "")
                touched.extend(_extract_package_id_from_path(fp))
            touched.extend(_extract_package_id_from_commit_message(msg))
            for pid in touched:
                if pid not in seen:
                    seen.append(pid)
            if len(seen) >= limit:
                break
        return seen[:limit]

    # ---- winget.run REST fallback ------------------------------------------
    async def _wingetrun_search(self, query: str, *, limit: int) -> list[PackageMetadata]:
        if self.mode == "github":
            raise WingetRunNotConfiguredError("search mode is github-only")
        url = f"{WINGET_RUN_API}/v2/packages"
        params = {
            "query": query,
            "take": limit,
            "splitQuery": "true",
            "partialMatch": "false",
        }
        data, _ = await self.http.fetch_json(
            url,
            params=params,
            cache_key=self.cache_key(f"wr:search:{query.lower().strip()}:{limit}"),
        )
        packages = data.get("Packages", []) if isinstance(data, dict) else []
        out: list[PackageMetadata] = []
        for raw in packages:
            meta = self._normalize_wingetrun_package(raw)
            if meta is not None:
                out.append(meta)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _normalize_wingetrun_package(raw: dict[str, Any]) -> PackageMetadata | None:
        pkg_id = raw.get("Id")
        if not pkg_id:
            return None
        latest = raw.get("Latest") or {}
        versions = raw.get("Versions") or []
        if versions:
            versions_sorted = sorted(versions, key=_version_sort_key, reverse=True)
        else:
            versions_sorted = []
        return PackageMetadata(
            source="winget",
            id=pkg_id,
            name=latest.get("Name") or pkg_id,
            publisher=latest.get("Publisher") or "",
            version=versions_sorted[0] if versions_sorted else (raw.get("Version") or ""),
            description=latest.get("Description") or "",
            homepage=latest.get("Homepage"),
            license=latest.get("License"),
            license_url=latest.get("LicenseUrl"),
            tags=latest.get("Tags") or [],
            update_date=raw.get("UpdatedAt") or raw.get("createdAt"),
            versions=versions_sorted,
            raw_data={"wingetrun": True},
        )

    # ---- Normalization -----------------------------------------------------
    @staticmethod
    def normalize(raw: Any) -> PackageMetadata:
        """Map a merged GitHub manifest payload to a PackageMetadata model."""
        if not isinstance(raw, dict):
            raise TypeError("WingetAdapter.normalize expects a dict")
        package_id: str = raw["package_id"]
        version: str = raw["version"]
        manifests: dict[str, Any] = raw.get("manifests") or {}

        version_manifest = manifests.get("version") or {}
        installer_manifest = manifests.get("installer") or {}
        locale = manifests.get("locale_en_us") or manifests.get("default_locale") or {}

        pid = version_manifest.get("PackageIdentifier") or package_id
        pkg_version = version_manifest.get("PackageVersion") or version

        installers, upgrade_behavior = _normalize_installers(installer_manifest, pkg_version)
        apps_entries = installer_manifest.get("AppsAndFeaturesEntries") or []

        upgrade_code: str | None = None
        if apps_entries:
            upgrade_code = apps_entries[0].get("UpgradeCode")

        release_notes_url = (
            locale.get("ReleaseNotesUrl") or installer_manifest.get("ReleaseNotesUrl")
        )
        release_notes = locale.get("ReleaseNotes")

        tree_manifest_path = _id_to_subpath(pid)
        return PackageMetadata(
            source="winget",
            id=pid,
            name=locale.get("PackageName") or pid,
            publisher=locale.get("Publisher") or "",
            version=pkg_version,
            description=(
                locale.get("Description") or locale.get("ShortDescription") or ""
            ),
            homepage=locale.get("PackageUrl") or locale.get("PublisherUrl"),
            license=locale.get("License"),
            license_url=locale.get("LicenseUrl"),
            release_notes_url=release_notes_url,
            release_notes=release_notes,
            tags=locale.get("Tags") or [],
            moniker=locale.get("Moniker"),
            update_date=None,
            gallery_url=(
                f"https://github.com/{WINGET_REPO}/tree/master/manifests/"
                f"{tree_manifest_path}"
            ),
            versions=[pkg_version],
            dependencies=_normalize_winget_dependencies(installer_manifest),
            installers=installers,
            raw_data={
                "manifest_type": version_manifest.get("ManifestType"),
                "manifest_version": version_manifest.get("ManifestVersion"),
                "upgrade_behavior": upgrade_behavior,
                "upgrade_code": upgrade_code,
                "moniker": locale.get("Moniker"),
                "protocols": installer_manifest.get("Protocols"),
                "file_extensions": installer_manifest.get("FileExtensions"),
            },
        )

    # ---- Static helpers ----------------------------------------------------
    @staticmethod
    def _looks_like_version(name: str) -> bool:
        if name in _NON_VERSION_DIRS:
            return False
        # Some version dirs look like "1.2.3" or "149.0.7827.156" or "1.0-beta"
        return bool(VERSION_RE.match(name))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _id_to_subpath(package_id: str) -> str:
    """``Microsoft.VisualStudio.2022.Community`` -> ``m/Microsoft/VisualStudio/2022/Community``.

    winget-pkgs nests every dot-segment of the PackageIdentifier as its own
    directory under ``manifests/{first_letter}/``.
    """
    if "." not in package_id:
        return f"{package_id[0].lower()}/{package_id}"
    segments = package_id.split(".")
    first_letter = segments[0][0].lower()
    return "/".join([first_letter, *segments])


def _version_sort_key(v: str) -> tuple[int, list[int], list[str]]:
    """Stable sort key that puts numeric versions > alpha prereleases."""
    nums: list[int] = []
    rest: list[str] = []
    parts = re.split(r"[.\-_]", v)
    for part in parts:
        if part.isdigit():
            nums.append(int(part))
        else:
            rest.append(part)
    return (len(nums) > 0, nums, rest)


def _normalize_installers(
    installer_manifest: dict[str, Any], default_version: str
) -> tuple[list[InstallerInfo], str | None]:
    """Convert an installer manifest's Installers[] to InstallerInfo models."""
    top_level = installer_manifest.get("InstallerType")
    default_scope = installer_manifest.get("Scope")
    upgrade_behavior = installer_manifest.get("UpgradeBehavior")
    top_switches = installer_manifest.get("InstallerSwitches") or {}

    out: list[InstallerInfo] = []
    for entry in installer_manifest.get("Installers", []) or []:
        switches = entry.get("InstallerSwitches") or top_switches or {}
        product_code = entry.get("ProductCode")
        # AppsAndFeaturesEntries may carry ProductCode/UpgradeCode per installer
        apps = entry.get("AppsAndFeaturesEntries") or []
        upgrade_code: str | None = None
        if apps:
            apps0 = apps[0] if isinstance(apps, list) else apps
            upgrade_code = apps0.get("UpgradeCode")
            if not product_code:
                product_code = apps0.get("ProductCode")

        installer_type = (
            entry.get("InstallerType") or top_level or "unknown"
        )
        arch = entry.get("Architecture") or "unknown"
        sha256 = entry.get("InstallerSha256")
        url = entry.get("InstallerUrl")
        if not url:
            continue
        out.append(
            InstallerInfo(
                url=url,
                sha256=(sha256.lower() if sha256 else None),
                installer_type=str(installer_type).lower(),
                architecture=str(arch).lower(),
                scope=entry.get("Scope") or default_scope,
                product_code=product_code,
                upgrade_code=upgrade_code,
                silent_switch=switches.get("Silent"),
                silent_with_progress_switch=switches.get("SilentWithProgress"),
                signature_sha256=entry.get("SignatureSha256"),
                abi=entry.get("Abi"),
            )
        )
    return out, upgrade_behavior


def _normalize_winget_dependencies(installer_manifest: dict[str, Any]) -> list[DependencyInfo]:
    deps_raw = installer_manifest.get("Dependencies") or {}
    # Dependencies.Packages eller/WindowsFeatures/ExternalDependencies/PackageDependencies...
    out: list[DependencyInfo] = []
    for key, value in deps_raw.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    out.append(
                        DependencyInfo(
                            id=item.get("Id") or item.get("PackageIdentifier") or "",
                            version=item.get("MinimumVersion"),
                        )
                    )
                elif isinstance(item, str):
                    out.append(DependencyInfo(id=item))
    return out


def _extract_package_id_from_path(path: str) -> list[str]:
    """``manifests/g/Google/Chrome/149.0.7827.156/Google.Chrome.yaml`` -> ['Google.Chrome'].

    Also handles multi-segment ids like
    ``manifests/m/Microsoft/VisualStudio/2022/Community/17.10.0/Microsoft.VisualStudio.2022.Community.yaml``.
    """
    if not path.startswith("manifests/"):
        return []
    parts = path.split("/")
    # manifests / letter / <publisher-and-package-segments...> / Version / file.yaml
    if len(parts) < 5:
        return []
    filename = parts[-1] if parts else ""
    # YAML filename repeats the full PackageIdentifier; prefer that since it
    # disambiguates multi-segment ids from arbitrary subdir names.
    if filename.endswith(".yaml"):
        base = filename[: -len(".yaml")]
        # Drop well-known manifest suffixes in reverse order, AND any
        # ``.locale.<tag>`` file (fr-FR, de-DE, ja-JP, ...) so we don't
        # return bogus multi-locale ids like ``Google.Chrome.locale.fr-FR``.
        if ".locale." in base:
            base = base[: base.find(".locale.")]
        else:
            for suffix in (".locale.default", ".locale.en-US", ".installer"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
        if "." in base:
            return [base]
    return []


def _extract_package_id_from_commit_message(msg: str) -> list[str]:
    """YamlCreate commit messages often include the package id, e.g.
    'Add ManifestIdentifier: Google.Chrome version 149.0.7827.156'."""
    out: list[str] = []
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9]+\.[A-Z][A-Za-z0-9.]+)\b", msg):
        candidate = match.group(1)
        # filter out obvious false positives like 'New.Version'
        if candidate in {"New.Version", "Add.ManifestIdentifier", "Version.", "Manifest.VerType"}:
            continue
        out.append(candidate)
    return out
