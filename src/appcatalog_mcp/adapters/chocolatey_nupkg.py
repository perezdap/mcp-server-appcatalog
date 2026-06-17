"""Chocolatey ``.nupkg`` installer-script parser.

The Chocolatey OData feed only exposes the ``.nupkg`` download URL — a ZIP that
contains the raw installer + a PowerShell install script. That script
(``tools/chocolateyInstall.ps1``) almost always holds the ground truth the
OData feed hides:

- the real ``fileType`` ('exe', 'msi', 'msix', ...)
- the ``silentArgs`` (e.g. ``/quiet /norestart``)
- for *remote-installer* packages: ``url`` / ``url64bit`` + matching
  ``checksum`` / ``checksum64`` (base64 or hex SHA256)
- the ``softwareName`` used for detection / uninstall string matching
- per-arch installer URLs when the package hosts them remotely

For *embedded-binary* packages (e.g. ``7zip.install`` with ``tools/7zip_x64.exe``
inside the .nupkg), there's no remote URL — we surface the fileType + silentArgs
+ the embedded binary filename, and keep the ``.nupkg`` URL as the downloadable.

This module is pure: input is the install-script text (or nuspec XML), output is
a normalized dict consumed by :class:`ChocolateyAdapter.get_installer_detail`.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)

# Regex notes:
#   - Chocolatey install scripts almost always use a single ``$packageArgs = @{...}``
#     hashtable. We split it on key/value pairs of the form ``key = 'value'`` and
#     capture every key. This is intentionally tolerant: brace bodies may span
#     many lines, may include expressions, and may use ``"`` or ``'`` quoting.
#   - The hashtable occasionally appears under different variable names
#     (``$installerArgs``, ``$params``...). We scan for ANY hashtable assignment
#     whose values look like installer keys and merge all of them.
_HASHTABLE_KEY_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*['\"](?P<val>[^'\"]*)['\"]",
    re.MULTILINE,
)
# Also match hashtable entries where the value is a multi-line expression
# terminating at a `,` or `}` — used for ``silentArgs`` paths with embedded
# sub-expressions. We only need the *leading quoted literal* (the actual args),
# which the simple regex above already captures.
_URL_KEYS = {
    "url": "x86",
    "url32": "x86",
    "url32bit": "x86",
    "url64": "x64",
    "url64bit": "x64",
    "urlarm64": "arm64",
    "urlarm": "arm",
}
_CHECKSUM_KEYS = {
    "checksum": "x86",
    "checksum32": "x86",
    "checksum32bit": "x86",
    "checksum64": "x64",
    "checksum64bit": "x64",
    "checksumarm64": "arm64",
    "checksumarm": "arm",
}
_CHECKSUM_TYPE_KEYS = {
    "checksumtype": "x86",
    "checksumtype32": "x86",
    "checksumtype64": "x64",
    "checksumtypearm64": "arm64",
    "checksumtypearm": "arm",
}

# Recognised InstallerType values from real Chocolatey packages.
_VALID_FILE_TYPES = {
    "exe", "msi", "msix", "msu", "appx", "appxbundle",
    "inno", "nsis", "wix", "burn", "portable", "zip", "7z",
}


def _to_lower_key_dict(keys: dict[str, str]) -> dict[str, str]:
    """Collapse hashtable keys to lowercase and de-duplicate (last wins)."""
    out: dict[str, str] = {}
    for k, v in keys.items():
        out[k.lower()] = v
    return out


def parse_install_script(ps1_text: str) -> dict[str, Any]:
    """Parse ``tools/chocolateyInstall.ps1`` content.

    Returns a dict shaped like::

        {
          "file_type": "MSI" | None,
          "silent_args": "/quiet /norestart" | None,
          "valid_exit_codes": [0, 3010] | None,
          "software_name": "7-zip*" | None,
          "arch_urls":   {"x86": url, "x64": url, "arm64": url, "arm": url},
          "arch_hashes": {"x86": sha256, "x64": sha256, ...},
          "arch_hash_is_sha256": {"x86": True, "x64": True, ...},
          "embedded_tools_binaries": ["7zip_x64.exe", "7zip_x32.exe"],
          "raw": {<all hashtable key/values>},
        }

    ``arch_hashes`` only carries hashes explicitly declared SHA512 (the
    algorithm determines whether we surface them as ``Sha256`` on
    :class:`InstallerInfo`` — if the algorithm is SHA512 we keep it in
    ``raw_data`` and leave ``sha256=None``).
    """
    out: dict[str, Any] = {
        "file_type": None,
        "silent_args": None,
        "valid_exit_codes": None,
        "software_name": None,
        "arch_urls": {},
        "arch_hashes": {},
        "arch_hash_is_sha256": {},
        "embedded_tools_binaries": [],
        "raw": {},
    }
    if not ps1_text:
        return out

    raw_keys = _extract_hashtable_keys(ps1_text)
    if not raw_keys:
        # Nothing structured found — best-effort regexes next.
        raw_keys = match_simple_keys(ps1_text)
    out["raw"] = raw_keys

    # validExitCodes often appears as an unquoted array literal,
    # ``validExitCodes = @(0, 3010, 1641)``, which the quoted-value regex
    # can't capture. Pull it directly from the script text first so we don't
    # depend on someone quoting the exit-codes list.
    codes = _parse_exit_codes_from_text(ps1_text)
    if codes:
        out["valid_exit_codes"] = codes

    if "filetype" in raw_keys:
        ft = raw_keys["filetype"].strip().strip("'\"")
        if ft and ft.lower() in _VALID_FILE_TYPES:
            out["file_type"] = ft
        elif ft:
            # Keep unchecked values too (uppercase MSI variants, etc.).
            out["file_type"] = ft

    if "silentargs" in raw_keys:
        silent = raw_keys["silentargs"].strip().strip("'\"")
        # PowerShell backtick line continuations / escaped quotes often leak
        # a trailing `` ` `` into the captured literal. Trim trailing
        # backticks so `` "/quiet /norestart /l*v ` `` becomes
        # `` /quiet /norestart /l*v `` (the trailing log path expression is
        # optional for packaging use and is dropped on purpose).
        silent = silent.rstrip("`").rstrip()
        out["silent_args"] = silent

    if "softwarename" in raw_keys:
        out["software_name"] = raw_keys["softwarename"].strip().strip("'\"")

    if "validexitcodes" in raw_keys and not out["valid_exit_codes"]:
        parsed_codes = _parse_exit_codes(raw_keys["validexitcodes"])
        if parsed_codes:
            out["valid_exit_codes"] = parsed_codes

    # Collect per-arch URLs + checksums.
    hash_algo_by_arch: dict[str, str | None] = {}
    for key, arch in _CHECKSUM_TYPE_KEYS.items():
        if key in raw_keys:
            hash_algo_by_arch[arch] = raw_keys[key].strip().lower()

    for key, arch in _URL_KEYS.items():
        if key in raw_keys:
            url = raw_keys[key].strip().strip("'\"")
            if url:
                out["arch_urls"][arch] = url

    for key, arch in _CHECKSUM_KEYS.items():
        if key in raw_keys:
            value = raw_keys[key].strip().strip("'\"").lower()
            if not value:
                continue
            algo = hash_algo_by_arch.get(arch) or "sha256"
            out["arch_hashes"][arch] = value
            out["arch_hash_is_sha256"][arch] = algo.startswith("sha256")

    # If the script references a tools-directory binary (embedded installer),
    # capture the filenames so callers can decide to download the .nupkg
    # itself to extract them. Only files under tools/ count.
    out["embedded_tools_binaries"] = _embedded_tools_bins(ps1_text)

    return out


def _extract_hashtable_keys(ps1_text: str) -> dict[str, str]:
    """Pull key/value pairs out of PowerShell hashtable assignments.

    Chocolatey install scripts use ``$name = @{ key = 'value' ; key2 = "value2" }``
    (or ``$numbered = \\n  @{ key = 'value' }``). We find every such assignment
    on a single source file basis by locating ``@{...}`` blocks first and then
    extracting every ``key = 'value'`` pair within each block.
    """
    keys: dict[str, str] = {}
    for block in _iter_hashtable_blocks(ps1_text):
        for match in _HASHTABLE_KEY_RE.finditer(block):
            k = match.group("key").lower()
            v = match.group("val")
            # Don't overwrite a meaningful ``url``/``checksum`` value with an
            # empty one (some scripts blank a key out conditionally).
            if not v and keys.get(k):
                continue
            keys[k] = v
    return keys


def _iter_hashtable_blocks(text: str):
    """Yield the inner text of each ``@{...}`` block in ``text`` (top-level only).

    Tracks brace depth so nested ``@{}`` and ``[...]`` bodies don't break
    extraction; bracket/brace ignores quote state robustly. About as good as
    PowerShell scripts get without a full tokenizer.
    """
    i = 0
    n = len(text)
    while i < n:
        # Locate next ``@{``
        idx = text.find("@{", i)
        if idx < 0:
            return
        start = idx + 2
        depth = 1
        j = start
        in_single = False
        in_double = False
        while j < n and depth > 0:
            ch = text[j]
            # Toggle quote state, taking care not to interpret an escaped quote.
            if ch == "'" and not in_double:
                prev = text[j - 1] if j > 0 else ""
                # PowerShell uses '' for an escaped single quote inside a string.
                if prev == "'" and j + 1 < n and text[j + 1] != "'":
                    pass
                in_single = not in_single
            elif ch == '"' and not in_single:
                # Allow ``""`` escape.
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[start:j]
                        i = j + 1
                        break
            j += 1
        else:
            # Ran off the end without closing — yield what we have.
            yield text[start:j]
            return
        if j >= n:
            return


def match_simple_keys(ps1_text: str) -> dict[str, str]:
    """Greedy fallback when no ``@{...}`` is found — grab every
    ``$key = 'value'`` assignment anywhere in the script."""
    keys: dict[str, str] = {}
    pattern = re.compile(
        r"\$(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*['\"](?P<val>[^'\"]*)['\"]"
    )
    for match in pattern.finditer(ps1_text):
        keys[match.group("key").lower()] = match.group("val")
    return keys


def _parse_exit_codes(raw_value: str) -> list[int] | None:
    """Parse ``@(0, 3010)`` → ``[0, 3010]``; tolerate stray ``@( … )`` form."""
    inside = raw_value.strip().lstrip("@(").rstrip(")").strip()
    if not inside:
        return None
    codes: list[int] = []
    for part in re.split(r"[,;\s]+", inside):
        if part.isdigit():
            codes.append(int(part))
    return codes or None


_EXIT_CODES_RE = re.compile(
    r"validExitCodes\s*=\s*@\s*\((?P<codes>[^)]*)\)",
    re.IGNORECASE,
)


def _parse_exit_codes_from_text(ps1_text: str) -> list[int] | None:
    """Pull ``validExitCodes = @(0, 3010, 1641)`` out of a script body."""
    match = _EXIT_CODES_RE.search(ps1_text)
    if not match:
        return None
    codes: list[int] = []
    for part in re.split(r"[,;\s]+", match.group("codes")):
        if part.isdigit():
            codes.append(int(part))
    return codes or None


_EMBEDDED_TOOLS_BIN_RE = re.compile(
    r"(?:\$toolsDir\\(?P<glob>[A-Za-z0-9_.?*]+\.[A-Za-z0-9]+)"
    r"|tools[/\\](?P<name>[A-Za-z0-9_.\-]+\.(?:exe|msi|msix|appx|msu|zip|7z)))",
    re.IGNORECASE,
)


def _embedded_tools_bins(ps1_text: str) -> list[str]:
    """Capture installer binary filenames referenced under ``tools/`` in scripts
    that embed the installer inside the .nupkg (e.g. ``7zip.install``)."""
    matches: list[str] = []
    seen: set[str] = set()
    for m in _EMBEDDED_TOOLS_BIN_RE.finditer(ps1_text):
        name = m.group("name") or m.group("glob")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            matches.append(name)
    return matches


def parse_nuspec_xml(nuspec_xml: str) -> dict[str, Any]:
    """Pull metadata from ``<package>/<metadata>`` of a .nuspec.

    Used to cross-check dependencies and tags against what the OData feed
    reports. Tolerant of namespaces.
    """
    out: dict[str, Any] = {"dependencies": [], "tags": []}
    if not nuspec_xml.strip():
        return out
    try:
        root = ET.fromstring(nuspec_xml)
    except ET.ParseError as exc:
        logger.debug("nuspec parse failed: %s", exc)
        return out
    # metadata can be namespaced; find by local name.
    metadata = _find_first_by_local(root, "metadata")
    if metadata is None:
        return out
    deps_node = _find_first_by_local(metadata, "dependencies")
    if deps_node is not None:
        for dep in deps_node:
            if _local_name(dep.tag) == "dependency":
                dep_id = dep.get("id") or dep.get("Id") or ""
                dep_ver = dep.get("version") or dep.get("Version")
                if dep_id:
                    out["dependencies"].append({"id": dep_id, "version": dep_ver})
    tags_node = _find_first_by_local(metadata, "tags")
    if tags_node is not None and tags_node.text:
        out["tags"] = [t.strip() for t in tags_node.text.split() if t.strip()]
    return out


def _find_first_by_local(parent: ET.Element, local_name: str) -> ET.Element | None:
    for el in parent.iter():
        if _local_name(el.tag) == local_name:
            return el
    return None


def _local_name(tag: str) -> str:
    # Strip XML namespace prefix if present (``{ns}tag`` → ``tag``).
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag
