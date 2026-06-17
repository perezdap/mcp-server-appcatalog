"""Chocolatey Community Repository adapter (OData v2 / Atom XML).

Endpoint: https://community.chocolatey.org/api/v2/

Notable quirks (verified empirically):
- The API serves Atom feed XML only (no JSON).
- ``Search()`` requires params in this exact set and order:
    searchTerm='vlc' & $filter=IsLatestVersion eq true & $skip=N & $top=M & includePrerelease=false
- ``Packages()?$filter=tolower(Id) eq '<id>'`` works for exact id lookup; sort
  by ``Version desc`` to pick the latest. ``substringof(...)`` does NOT work.
- The download URL points to a ``.nupkg`` zip (chocolateyInstall.ps1 wrapper),
  not the raw installer. We label this clearly in ``installer_type``.
- ``PackageHash`` is base64-encoded SHA512 (Algorithm='SHA512'), stored in
  ``sha256`` field as None and surfaced in ``raw_data``.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote, urlencode

from appcatalog_mcp.adapters.base import PackageAdapter, PackageNotFoundError
from appcatalog_mcp.adapters.chocolatey_nupkg import parse_install_script
from appcatalog_mcp.http_client import HttpClient, HttpClientError
from appcatalog_mcp.models import (
    DependencyInfo,
    InstallerInfo,
    PackageMetadata,
)

logger = logging.getLogger(__name__)

def _odata_literal(value: str) -> str:
    """Escape a value for embedding in an OData string literal (quotes doubled).

    OData v2 string literals are wrapped in single quotes; an embedded single
    quote must be doubled to stay inside the literal. ``urlencode`` percent-
    encodes the quote for transport but the server decodes it back, so without
    this escape the value can break out of the literal.
    """
    return value.replace("'", "''")


# Atom + OData namespaces used by the Chocolatey feeds.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}


def _qname(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


_ENTRY_RE = re.compile(r"<entry\b.*?</entry>", re.DOTALL | re.IGNORECASE)


def _recover_entries(xml: str) -> str:
    """Concatenate any complete ``<entry>...</entry>`` blocks from a truncated feed."""
    return "".join(_ENTRY_RE.findall(xml))


def _enrich_with_parsed_install_script(
    pkg: PackageMetadata,
    nupkg_url: str,
    parsed: dict[str, Any],
) -> PackageMetadata:
    """Merge parsed install-script data into a Chocolatey PackageMetadata.

    Replaces the placeholder ``.nupkg`` installer with one real per-arch
    :class:`InstallerInfo` per parsed ``url``/``url64bit``/… (with checksums
    when SHA256). For embedded-binary packages (no remote URLs), keeps the
    ``.nupkg`` URL but fills in ``installer_type``/``silent_switch`` from the
    script. Preserves the parsed PS1 fields in ``raw_data["install_script"]``.
    """
    file_type = (parsed.get("file_type") or "nupkg").lower()
    silent_args = parsed.get("silent_args")
    software_name = parsed.get("software_name")
    valid_exit_codes = parsed.get("valid_exit_codes")
    embedded_bins = parsed.get("embedded_tools_binaries") or []
    arch_urls = parsed.get("arch_urls") or {}
    arch_hashes = parsed.get("arch_hashes") or {}
    hash_is_sha256 = parsed.get("arch_hash_is_sha256") or {}

    installers: list[InstallerInfo] = []
    if arch_urls:
        # Remote-installer package: one InstallerInfo per arch URL.
        for arch, url in arch_urls.items():
            sha256 = None
            raw_hash = arch_hashes.get(arch)
            is_sha256 = hash_is_sha256.get(arch, True)
            if raw_hash and is_sha256:
                sha256 = raw_hash.lower()
            installers.append(
                InstallerInfo(
                    url=url,
                    sha256=sha256,
                    installer_type=file_type,
                    architecture=arch,
                    scope=None,
                    product_code=None,
                    upgrade_code=None,
                    silent_switch=silent_args,
                    file_size=None,
                )
            )
    else:
        # Embedded-binary package (e.g. 7zip.install): keep the .nupkg URL as the
        # downloadable but fill in the real installer type + silent switches.
        installers.append(
            InstallerInfo(
                url=nupkg_url,
                sha256=None,
                installer_type=file_type,
                architecture="neutral",
                scope=None,
                product_code=None,
                upgrade_code=None,
                silent_switch=silent_args,
                file_size=None,
            )
        )

    raw_data = dict(pkg.raw_data or {})
    raw_data["install_script"] = {
        "file_type": file_type,
        "silent_args": silent_args,
        "software_name": software_name,
        "valid_exit_codes": valid_exit_codes,
        "embedded_tools_binaries": embedded_bins,
        "note": (
            "Parsed from tools/chocolateyInstall.ps1 inside the .nupkg. "
            "For embedded-binary packages the .nupkg URL points at the zip "
            "that contains the real installer under tools/."
        ),
    }
    return pkg.model_copy(
        update={
            "installers": installers,
            "raw_data": raw_data,
        }
    )


class ChocolateyAdapter(PackageAdapter):
    """Adapter for the Chocolatey Community Repository OData v2 API."""

    name = "chocolatey"
    display_label = "Chocolatey"

    def __init__(self, http: HttpClient, api_base: str) -> None:
        super().__init__(http)
        self.api_base = api_base.rstrip("/")

    # ---- Public API --------------------------------------------------------
    async def search(self, query: str, *, limit: int = 10) -> list[PackageMetadata]:
        limit = max(1, min(limit, 100))
        cache_key = self.cache_key(f"search:{query.lower().strip()}:{limit}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return [PackageMetadata.model_validate(p) for p in cached]

        params = {
            "searchTerm": f"'{_odata_literal(query)}'",
            "$filter": "IsLatestVersion eq true",
            "$skip": 0,
            "$top": limit,
            "includePrerelease": "false",
        }
        url = f"{self.api_base}/Search()?{urlencode(params, quote_via=quote)}"
        # Chocolatey strictly validates Accept header — must include atom+xml.
        xml, _ = await self.http.fetch_text(
            url,
            headers={"Accept": "application/atom+xml,application/xml"},
            cache_key=self.cache_key(f"search-xml:{query.lower().strip()}:{limit}"),
        )
        entries = self._parse_feed(xml)
        out = [self.normalize(e) for e in entries]
        # Re-rank by a simple id/tag/title match score (since the server search
        # already orders by relevance, just truncate to limit).
        results = out[:limit]
        self.http.set_cache(cache_key, [p.model_dump(mode="json") for p in results])
        return results

    async def get_package(
        self, package_id: str, *, version: str | None = None
    ) -> PackageMetadata:
        pid = package_id.strip()
        cache_key = self.cache_key(f"pkg:{pid.lower()}:{version or 'latest'}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            meta = PackageMetadata.model_validate(cached)
            meta.cache_hit = True
            return meta

        if version is None:
            entry = await self._fetch_latest_entry(pid)
        else:
            entry = await self._fetch_version_entry(pid, version)
        if entry is None:
            raise PackageNotFoundError(f"chocolatey package {package_id!r} not found")
        meta = self.normalize(entry)
        # For the latest we also attach the known-versions list (top N).
        if version is None:
            meta.versions = await self._fetch_versions(pid)
        else:
            meta.versions = [version]
        self.http.set_cache(cache_key, meta.model_dump(mode="json"))
        return meta

    async def get_installer_detail(
        self, package_id: str, version: str | None = None
    ) -> PackageMetadata:
        """Deep dive: download the `.nupkg`, parse `tools/chocolateyInstall.ps1`,
        and surface the real installer URLs + SHA256 hashes + silent args that
        the OData feed hides.

        Falls back gracefully to the OData-only record (with the `.nupkg` URL as
        the installer) if the .nupkg can't be fetched or has no
        `tools/chocolateyInstall.ps1`.
        """
        pkg = await self.get_package(package_id, version=version)
        # The OData-only record has at most one installer pointing at the
        # .nupkg. If we can enrich it with parsed install-script data, replace
        # that stub with the real per-arch installer URLs.
        nupkg_url = next(
            (i.url for i in pkg.installers if i.installer_type == "nupkg"),
            None,
        )
        if not nupkg_url:
            return pkg

        cache_key = self.cache_key(
            f"nupkg:{package_id.lower()}:{pkg.version}"
        )
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            parsed: dict[str, Any] | None = cached
        else:
            parsed = await self._fetch_and_parse_nupkg(nupkg_url)
            if parsed is not None:
                self.http.set_cache(cache_key, parsed)

        if not parsed:
            return pkg

        enriched = _enrich_with_parsed_install_script(pkg, nupkg_url, parsed)
        return enriched

    async def _fetch_and_parse_nupkg(
        self, nupkg_url: str
    ) -> dict[str, Any] | None:
        """Fetch the .nupkg over HTTP, unzip ``tools/chocolateyInstall.ps1`` in
        memory, parse it. Returns ``None`` on any failure or if the member
        is absent.
        """
        try:
            ps1_bytes = await self.http.fetch_zip_member(
                nupkg_url,
                "tools/chocolateyInstall.ps1",
                cache_key=f"choco:nupkg-bytes:{nupkg_url}",
            )
        except HttpClientError as exc:
            logger.debug("%s: no tools/chocolateyInstall.ps1 (%s)", nupkg_url, exc)
            return None
        try:
            ps1_text = ps1_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            logger.warning("nupkg PS1 decode failed for %s: %s", nupkg_url, exc)
            return None
        parsed = parse_install_script(ps1_text)
        parsed["_nupkg_url"] = nupkg_url
        return parsed


    async def list_recent(self, *, limit: int = 10) -> list[PackageMetadata]:
        limit = max(1, min(limit, 100))
        cache_key = self.cache_key(f"recent:{limit}")
        cached = self.http.get_cache(cache_key)
        if cached is not None:
            return [PackageMetadata.model_validate(p) for p in cached]

        params = {
            "$filter": "IsLatestVersion eq true",
            "$orderby": "Published desc",
            "$top": limit,
        }
        url = f"{self.api_base}/Packages()?{urlencode(params, quote_via=quote)}"
        xml, _ = await self.http.fetch_text(
            url,
            headers={"Accept": "application/atom+xml,application/xml"},
            cache_key=self.cache_key(f"recent-xml:{limit}"),
        )
        entries = self._parse_feed(xml)
        results = [self.normalize(e) for e in entries][:limit]
        self.http.set_cache(cache_key, [p.model_dump(mode="json") for p in results])
        return results

    # ---- Fetch helpers -----------------------------------------------------
    async def _fetch_latest_entry(self, package_id: str) -> ET.Element | None:
        # IsLatestVersion eq true is the server-marked "latest" entry. Combined
        # with an id filter this reliably returns the newest published version
        # regardless of lexicographic Version ordering quirks.
        safe_id = _odata_literal(package_id.lower())
        params = {
            "$filter": f"tolower(Id) eq '{safe_id}' and IsLatestVersion eq true",
            "$top": 1,
        }
        url = f"{self.api_base}/Packages()?{urlencode(params, quote_via=quote)}"
        try:
            xml, _ = await self.http.fetch_text(
                url,
                headers={"Accept": "application/atom+xml,application/xml"},
                cache_key=self.cache_key(f"latest:{package_id.lower()}"),
            )
        except HttpClientError as exc:
            logger.debug("Chocolatey latest fetch failed for %s: %s", package_id, exc)
            return None
        entries = self._parse_feed(xml)
        return entries[0] if entries else None

    async def _fetch_version_entry(
        self, package_id: str, version: str
    ) -> ET.Element | None:
        # OData function invocation: GetPackage?... no — use Packages() with version filter.
        safe_id = _odata_literal(package_id.lower())
        safe_version = _odata_literal(version)
        params = {
            "$filter": f"tolower(Id) eq '{safe_id}' and Version eq '{safe_version}'",
            "$top": 1,
        }
        url = f"{self.api_base}/Packages()?{urlencode(params, quote_via=quote)}"
        try:
            xml, _ = await self.http.fetch_text(
                url,
                headers={"Accept": "application/atom+xml,application/xml"},
                cache_key=self.cache_key(f"ver:{package_id.lower()}:{version}"),
            )
        except HttpClientError as exc:
            logger.debug("Chocolatey version fetch failed for %s/%s: %s", package_id, version, exc)
            return None
        entries = self._parse_feed(xml)
        return entries[0] if entries else None

    async def _fetch_versions(self, package_id: str) -> list[str]:
        safe_id = _odata_literal(package_id.lower())
        params = {
            "$filter": f"tolower(Id) eq '{safe_id}'",
            "$orderby": "Version desc",
            "$top": 30,
        }
        url = f"{self.api_base}/Packages()?{urlencode(params, quote_via=quote)}"
        try:
            xml, _ = await self.http.fetch_text(
                url,
                headers={"Accept": "application/atom+xml,application/xml"},
                cache_key=self.cache_key(f"versions:{package_id.lower()}"),
            )
        except HttpClientError as exc:
            logger.debug("Chocolatey versions fetch failed: %s", exc)
            return []
        entries = self._parse_feed(xml)
        raw_versions: list[str] = [self._prop(e, "Version") or "" for e in entries]
        # OData $orderby=Version desc is lexicographic, which misorders multi-digit
        # segments ("9.0.0" vs "26.1.0"). Re-sort client-side using semver tuples.
        from appcatalog_mcp.adapters.winget_adapter import _version_sort_key
        return sorted(
            [v for v in raw_versions if v],
            key=_version_sort_key,
            reverse=True,
        )

    # ---- Atom parsing ------------------------------------------------------
    @staticmethod
    def _parse_feed(xml: str) -> list[ET.Element]:
        """Parse an OData Atom feed, tolerating Chocolatey's truncated responses.

        Chocolatey's Search()/Packages() endpoints occasionally emit a valid feed
        followed by a stray ``<m:error>Object reference not set...</m:error>`` block,
        which produces invalid XML. We try strict parsing first; on failure we
        extract ``<entry>...</entry>`` blocks via regex and re-wrap them in a
        synthetic feed document.
        """
        if not xml.strip():
            return []
        entries: list[ET.Element] = []
        try:
            root = ET.fromstring(xml)
            candidates = root.findall(".//" + _qname("atom", "entry"))
            if not candidates:
                candidates = root.findall(".//entry")
            entries = candidates
        except ET.ParseError as exc:
            logger.debug("Chocolatey strict parse failed (%s); recovering", exc)
            recovered = _recover_entries(xml)
            if recovered:
                try:
                    root = ET.fromstring(
                        f"<feed xmlns=\"http://www.w3.org/2005/Atom\">{recovered}</feed>"
                    )
                    entries = root.findall(".//" + _qname("atom", "entry"))
                    if not entries:
                        entries = root.findall(".//entry")
                except ET.ParseError as inner:
                    logger.warning("Chocolatey feed recovery failed: %s", inner)
        return entries

    @staticmethod
    def _prop(entry: ET.Element, name: str) -> str | None:
        """Return text of a ``<d:Name>`` property under ``<m:properties>``."""
        props = entry.find(".//" + _qname("m", "properties"))
        if props is None:
            return None
        node = props.find(_qname("d", name))
        if node is None or node.text is None:
            return None
        return node.text

    @staticmethod
    def _atom_text(entry: ET.Element, tag: str) -> str | None:
        node = entry.find(_qname("atom", tag))
        if node is None or node.text is None:
            return None
        return node.text

    @staticmethod
    def _atom_attr(entry: ET.Element, tag: str, attr: str) -> str | None:
        node = entry.find(_qname("atom", tag))
        if node is None:
            return None
        return node.get(attr)

    @staticmethod
    def _author(entry: ET.Element) -> str | None:
        node = entry.find(_qname("atom", "author"))
        if node is None:
            return None
        name_node = node.find(_qname("atom", "name"))
        return name_node.text if name_node is not None and name_node.text else None

    # ---- Normalization -----------------------------------------------------
    @classmethod
    def normalize(cls, entry: ET.Element) -> PackageMetadata:
        """Map a Chocolatey OData <entry> to PackageMetadata."""
        package_id = cls._atom_text(entry, "title") or ""
        # entry <id> contains "(Id='...',Version='...')"
        version = cls._prop(entry, "Version") or ""
        title = cls._prop(entry, "Title") or package_id
        summary = cls._atom_text(entry, "summary") or ""
        description = cls._prop(entry, "Description") or summary or ""
        nupkg_url = cls._atom_attr(entry, "content", "src") or ""
        project_url = cls._prop(entry, "ProjectUrl")
        license_url = cls._prop(entry, "LicenseUrl")
        release_notes = cls._prop(entry, "ReleaseNotes")
        tags_raw = cls._prop(entry, "Tags") or ""
        tags = [t.strip() for t in tags_raw.split() if t.strip()]
        deps_raw = cls._prop(entry, "Dependencies") or ""
        dependencies = cls._parse_dependencies(deps_raw)

        package_hash = cls._prop(entry, "PackageHash")
        package_hash_algo = cls._prop(entry, "PackageHashAlgorithm") or "SHA512"
        package_size_raw = cls._prop(entry, "PackageSize")
        package_size: int | None = None
        if package_size_raw and package_size_raw.isdigit():
            package_size = int(package_size_raw)

        download_count_raw = cls._prop(entry, "DownloadCount")
        download_count: int | None = None
        if download_count_raw and download_count_raw.isdigit():
            download_count = int(download_count_raw)

        installer = InstallerInfo(
            url=nupkg_url,
            sha256=None,  # Chocolatey uses SHA512; surface in raw_data instead
            installer_type="nupkg",
            architecture="neutral",
            scope=None,
            product_code=None,
            upgrade_code=None,
            file_size=package_size,
        )

        raw_data: dict[str, Any] = {
            "package_hash": package_hash,
            "package_hash_algorithm": package_hash_algo,
            "is_absolute_latest_version": cls._prop(entry, "IsAbsoluteLatestVersion"),
            "is_prerelease": cls._prop(entry, "IsPrerelease"),
            "gallery_details_url": cls._prop(entry, "GalleryDetailsUrl"),
            "icon_url": cls._prop(entry, "IconUrl"),
            "package_source_url": cls._prop(entry, "PackageSourceUrl"),
            "docs_url": cls._prop(entry, "DocsUrl"),
            "bug_tracker_url": cls._prop(entry, "BugTrackerUrl"),
            "mailing_list_url": cls._prop(entry, "MailingListUrl"),
            "note": (
                "Download URL points to the .nupkg zip (chocolateyInstall.ps1 + tools), "
                "not a raw installer URL. Unzip + run choco, or inspect tools/ for the "
                "embedded installer and its silent switches."
            ),
        }

        is_release_url = bool(release_notes) and release_notes.startswith("http")
        return PackageMetadata(
            source="chocolatey",
            id=package_id,
            name=title or package_id,
            publisher=cls._author(entry) or "",
            version=version,
            description=description,
            homepage=project_url,
            license=license_url,  # No free-text license field; license_url is most useful
            license_url=license_url,
            release_notes_url=release_notes if is_release_url else None,
            release_notes=None if is_release_url else release_notes,
            tags=tags,
            moniker=None,
            update_date=cls._atom_text(entry, "updated"),
            gallery_url=cls._prop(entry, "GalleryDetailsUrl")
            or f"https://community.chocolatey.org/packages/{package_id}/{version}",
            download_count=download_count,
            dependencies=dependencies,
            installers=[installer] if nupkg_url else [],
            versions=[version],
            raw_data=raw_data,
        )

    @staticmethod
    def _parse_dependencies(deps_raw: str) -> list[DependencyInfo]:
        """Chocolatey Dependencies field format: ``id1:[ver1]:|id2:[ver2]:|...``"""
        if not deps_raw:
            return []
        out: list[DependencyInfo] = []
        for chunk in deps_raw.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            # Each chunk like "vlc.install:[3.0.23]:"
            id_part, _, ver_block = chunk.partition(":")
            id_stripped = id_part.strip()
            if not id_stripped:
                continue
            version: str | None = None
            # ver_block like "[3.0.23]:" — strip brackets/extras
            ver_block_inner = ver_block.strip(":").strip()
            if ver_block_inner.startswith("["):
                ver_block_inner = ver_block_inner[1:-1]
            if ver_block_inner:
                # Spec may be "[1.0, 2.0)" — strip range chars.
                version = ver_block_inner.strip("()[] ")
            out.append(DependencyInfo(id=id_stripped, version=version or None))
        return out
