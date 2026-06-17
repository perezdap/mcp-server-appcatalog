"""Unit tests for the Chocolatey ``.nupkg`` install-script parser."""

from __future__ import annotations

from pathlib import Path

from appcatalog_mcp.adapters.chocolatey_nupkg import (
    parse_install_script,
    parse_nuspec_xml,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------


def test_parse_remote_installer_package_googlechrome():
    """Google Chrome's nupkg surfaces real per-arch MSI URLs + SHA256."""
    ps1 = _read("choco_nupkg_googlechrome.chocolateyInstall.ps1")
    parsed = parse_install_script(ps1)
    assert parsed["file_type"] == "MSI"
    assert parsed["silent_args"] == "/quiet /norestart /l*v"
    assert parsed["arch_urls"]["x86"].endswith("googlechromestandaloneenterprise.msi")
    assert parsed["arch_urls"]["x64"].endswith("googlechromestandaloneenterprise64.msi")
    # SHA256 hashes from the install script match winget's manifest exactly —
    # they're the real Chrome MSI hashes Google publishes.
    assert (
        parsed["arch_hashes"]["x86"]
        == "ae9ba8c2ca5ea4e46d0a33f30524a23a484d979df82cbe5c309b0406e43bfe2d"
    )
    assert (
        parsed["arch_hashes"]["x64"]
        == "2c40bd99951a5c9270bde3cae7ab7be3e82570e2bced911a5f8bd4a23af252c9"
    )
    assert parsed["arch_hash_is_sha256"]["x86"] is True
    assert parsed["arch_hash_is_sha256"]["x64"] is True
    assert parsed["embedded_tools_binaries"] == []


def test_parse_embedded_binary_package_7zip_install():
    """7-Zip's install package embeds the raw .exe inside the .nupkg (no remote URL)."""
    ps1 = _read("choco_nupkg_7zip.install.chocolateyInstall.ps1")
    parsed = parse_install_script(ps1)
    assert parsed["file_type"] == "exe"
    assert parsed["silent_args"] == "/S"
    assert parsed["software_name"] == "7-zip*"
    # No remote URLs — embedded binaries under tools/ instead.
    assert parsed["arch_urls"] == {}
    assert parsed["arch_hashes"] == {}
    # Embedded binary globs captured (used by callers that want to extract
    # the real installer from the .nupkg zip).
    assert "*_x64.exe" in parsed["embedded_tools_binaries"]
    assert "*_x32.exe" in parsed["embedded_tools_binaries"]


def test_parse_empty_script():
    parsed = parse_install_script("")
    assert parsed["file_type"] is None
    assert parsed["silent_args"] is None
    assert parsed["arch_urls"] == {}


def test_parse_url64bit_alias():
    """``url64bit`` (not ``url64``) is the canonical Chocolatey key spelling."""
    ps1 = (
        "$packageArgs = @{\n"
        "  fileType       = 'msi'\n"
        "  url            = 'https://example.com/x86.msi'\n"
        "  url64bit       = 'https://example.com/x64.msi'\n"
        "  checksum       = 'abc123'\n"
        "  checksum64     = 'def456'\n"
        "  checksumType    = 'sha256'\n"
        "  silentArgs     = '/qn'\n"
        "}\n"
    )
    parsed = parse_install_script(ps1)
    assert parsed["file_type"] == "msi"
    assert parsed["silent_args"] == "/qn"
    assert parsed["arch_urls"] == {
        "x86": "https://example.com/x86.msi",
        "x64": "https://example.com/x64.msi",
    }
    assert parsed["arch_hashes"]["x86"] == "abc123"
    assert parsed["arch_hashes"]["x64"] == "def456"


def test_parse_sha512_algorithm_is_not_sha256():
    """If checksumType is SHA512, surface hash but mark is_sha256=False."""
    ps1 = (
        "$args = @{\n"
        "  fileType       = 'msi'\n"
        "  url            = 'https://example.com/x86.msi'\n"
        "  checksum       = 'AAAA'\n"
        "  checksumType    = 'sha512'\n"
        "}\n"
    )
    parsed = parse_install_script(ps1)
    assert parsed["arch_hashes"]["x86"] == "aaaa"
    assert parsed["arch_hash_is_sha256"]["x86"] is False


def test_parse_valid_exit_codes():
    ps1 = (
        "$args = @{\n"
        "  validExitCodes = @(0, 3010, 1641)\n"
        "}\n"
    )
    parsed = parse_install_script(ps1)
    assert parsed["valid_exit_codes"] == [0, 3010, 1641]


def test_parse_fallback_simple_assignments():
    """When no @{...} block is present, fall back to greedy key=value scanning."""
    ps1 = (
        "$url = 'https://example.com/x86.exe'\n"
        "$silentArgs = '/S'\n"
        "$fileType = 'exe'\n"
    )
    parsed = parse_install_script(ps1)
    assert parsed["file_type"] == "exe"
    assert parsed["silent_args"] == "/S"


def test_parse_nuspec_xml_dependencies_and_tags():
    """.nuspec XML carries dependencies and tags (cross-check vs OData feed)."""
    nuspec = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">\n'
        '  <metadata>\n'
        '    <id>GoogleChrome</id>\n'
        '    <tags>google chrome web internet browser admin</tags>\n'
        '    <dependencies>\n'
        '      <dependency id="chocolatey-core.extension" version="1.3.3" />\n'
        '    </dependencies>\n'
        '  </metadata>\n'
        '</package>\n'
    )
    parsed = parse_nuspec_xml(nuspec)
    assert "google" in parsed["tags"]
    assert parsed["dependencies"][0]["id"] == "chocolatey-core.extension"
    assert parsed["dependencies"][0]["version"] == "1.3.3"


def test_parse_nuspec_empty_returns_empty_dict():
    parsed = parse_nuspec_xml("")
    assert parsed == {"dependencies": [], "tags": []}


def test_parse_nuspec_invalid_xml_returns_empty():
    parsed = parse_nuspec_xml("<<not xml>>")
    assert parsed == {"dependencies": [], "tags": []}
