"""Tests for the find_best_source ranker and id-translation helpers."""

from __future__ import annotations

from appcatalog_mcp.models import (
    CandidateScore,
    InstallerInfo,
    PackageMetadata,
)
from appcatalog_mcp.tools.catalog import (
    _cross_source_id_candidates,
    _score_package,
)


def _winget_like_pkg(
    *,
    installers: int = 1,
    has_sha: bool = True,
    has_silent: bool = True,
    has_product_code: bool = True,
    has_upgrade_code: bool = True,
    has_homepage: bool = True,
    has_release_notes: bool = True,
    has_license: bool = True,
) -> PackageMetadata:
    return PackageMetadata(
        source="winget",
        id="X.Y",
        name="X",
        publisher="X",
        version="1.0.0",
        description="",
        homepage="https://x.example" if has_homepage else None,
        license="MIT" if has_license else None,
        license_url="https://x.example/license" if has_license else None,
        release_notes_url="https://x.example/notes" if has_release_notes else None,
        installers=[
            InstallerInfo(
                url=f"https://x.example/{i}.msi",
                sha256=f"hash{i:060x}" if has_sha else None,
                installer_type="msi",
                architecture="x64",
                product_code=f"{{PC-{i}}}" if has_product_code else None,
                upgrade_code="{UC}" if has_upgrade_code and i == 0 else None,
                silent_switch="/quiet" if has_silent else None,
            )
            for i in range(installers)
        ],
    )


# ---- _score_package --------------------------------------------------------


def test_score_full_winget_package_is_high():
    pkg = _winget_like_pkg(installers=3)
    score, reasons = _score_package(pkg)
    assert score >= 15
    assert any("SHA256" in r for r in reasons)
    assert any("product code" in r for r in reasons)


def test_score_rewards_hashes():
    with_hash = _winget_like_pkg(
        installers=2, has_sha=True, has_product_code=False, has_upgrade_code=False,
    )
    without_hash = _winget_like_pkg(
        installers=2, has_sha=False, has_product_code=False, has_upgrade_code=False,
    )
    assert _score_package(with_hash)[0] > _score_package(without_hash)[0]


def test_score_rewards_product_code_for_intune():
    with_pc = _winget_like_pkg(has_product_code=True, has_silent=False, has_sha=False)
    without_pc = _winget_like_pkg(has_product_code=False, has_silent=False, has_sha=False)
    assert _score_package(with_pc)[0] > _score_package(without_pc)[0]


def test_score_evergreen_gets_freshness_bonus():
    ev_pkg = PackageMetadata(
        source="evergreen",
        id="7zip",
        name="7zip",
        publisher="",
        version="26.01",
        installers=[
            InstallerInfo(
                url="https://x", sha256="abc",
                installer_type="exe", architecture="x64",
            )
        ],
    )
    ch_pkg = PackageMetadata(
        source="chocolatey",
        id="7zip",
        name="7zip",
        publisher="",
        version="26.1.0",
        installers=[
            InstallerInfo(
                url="https://x", sha256="abc",
                installer_type="exe", architecture="x64",
            )
        ],
    )
    # Identical installer metadata except source; Evergreen fresher-typed.
    assert _score_package(ev_pkg)[0] > _score_package(ch_pkg)[0]


def test_score_empty_package_is_zero():
    pkg = PackageMetadata(
        source="winget",
        id="x",
        name="x",
        publisher="",
        version="0",
        installers=[],
    )
    score, reasons = _score_package(pkg)
    assert score == 0
    assert "no installer(s)" not in reasons  # we don't currently emit a negative reason


# ---- _cross_source_id_candidates ------------------------------------------


def test_candidates_for_single_segment_id():
    assert _cross_source_id_candidates("7zip") == ["7zip"]


def test_candidates_for_two_segment_winget_id():
    """``Google.Chrome`` → [exact, joined lowercase, last lower, last title, last2 joined, etc.]"""
    out = _cross_source_id_candidates("Google.Chrome")
    assert out[0] == "Google.Chrome"
    assert "googlechrome" in out
    assert "chrome" in out
    assert "Chrome" in out


def test_candidates_include_choco_form_for_7zip():
    """``7zip.7zip`` → ``7zip`` (choco's exact id) is a candidate."""
    out = _cross_source_id_candidates("7zip.7zip")
    assert "7zip" in out
    assert "7zip7zip" in out


def test_candidates_dedup_preserves_order():
    out = _cross_source_id_candidates("Foo.Foo")
    # Exact-string uniqueness (NOT case-collapsing) — different casings route
    # to different Evergreen URLs, so they're kept distinct.
    assert len(out) == len(set(out))
    assert "Foo.Foo" in out


def test_candidate_count_bounded_for_multisegment_id():
    out = _cross_source_id_candidates("Microsoft.VisualStudio.2022.Community")
    # Reasonable upper bound — we don't explode the candidate list.
    assert len(out) <= 8
    assert "Microsoft.VisualStudio.2022.Community" in out
    assert "microsoftvisualstudio2022community" in out


# ---- CandidateScore round-trip -------------------------------------------


def test_candidate_score_model_round_trips():
    cand = CandidateScore(source="winget", score=10, reasons=["x"])
    dumped = cand.model_dump(mode="json")
    assert dumped["source"] == "winget"
    assert dumped["score"] == 10
