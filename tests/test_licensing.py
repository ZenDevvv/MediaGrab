"""The project is GPL-3.0 and ships its licence.

These exist because the license state of this repository was, until recently,
described only in a README sentence: no LICENSE file, no NOTICE, and a PRD
entry tracking FFmpeg's GPL build as an unresolved blocker on publishing.

That is a quietly bad state. MIT permits nothing without the notice being
conveyed, so the README sentence was not a licence grant -- it was a claim.
And GPL-3.0 section 4 requires the licence to accompany the *distributed work*,
which means being correctly licensed in the repository is necessary but not
sufficient: the files have to be in the bundle too.
"""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --- the licence itself ------------------------------------------------------


def test_a_license_file_exists_and_is_the_real_gpl3():
    """Not a stub, not a pointer to a URL.

    GPL-3.0 is a licence you have to convey in full; a summary is not a
    licence. The SHA-256 of the canonical FSF text is pinned so an edit here is
    a deliberate act rather than an accident.
    """
    license_path = ROOT / "LICENSE"
    assert license_path.exists(), (
        "No LICENSE file. The README claiming 'MIT' conveyed no grant -- MIT "
        "requires the copyright and permission notice in all copies."
    )

    text = license_path.read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text

    # The canonical text contains these distinctive sections; a paraphrase
    # would not, and paraphrasing a licence is legally meaningless.
    for heading in ("Preamble", "TERMS AND CONDITIONS", "NO WARRANTY",
                    "How to Apply These Terms to Your New Programs"):
        assert heading in text, f"GPL-3.0 is missing the {heading} section"

    digest = hashlib.sha256(text.encode()).hexdigest()
    assert digest == EXPECTED_GPL3_SHA256, (
        "LICENSE is not the verbatim FSF text (it should be byte-identical to "
        "https://www.gnu.org/licenses/gpl-3.0.txt). If upstream changed, "
        "re-download it and update EXPECTED_GPL3_SHA256; do not edit it by "
        f"hand. Expected {EXPECTED_GPL3_SHA256}, got {digest}"
    )


# sha256 of https://www.gnu.org/licenses/gpl-3.0.txt as fetched on 2026-10-13.
EXPECTED_GPL3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


def test_copyright_notice_conveys_the_grant():
    """The standard GPL-3.0 application notice, with a real holder and year.

    'Copyright (C) 2026 <nobody>' or a missing year is the shape of notice
    that has never actually been checked.
    """
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "GNU General Public License" in notice

    match = re.search(r"^Copyright \(C\) (\d{4}) (.+)$", notice, re.M)
    assert match, "NOTICE must state a copyright year and holder"

    year, holder = int(match.group(1)), match.group(2).strip()
    assert 2020 <= year <= 2026, f"implausible copyright year: {year}"
    assert holder and holder.lower() not in {"", "unknown", "todo", "xxx"}
    assert "complete corresponding source" in notice


def test_notice_inventories_every_bundled_third_party_component():
    """A GPL NOTICE that omits a bundled component does not discharge the
    obligation to preserve attribution."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

    for component, licence in (
        ("FFmpeg", "GPL-3.0"),
        ("Deno", "Apache-2.0"),
        ("yt-dlp", "Unlicense"),
    ):
        assert component in notice, f"{component} is bundled but absent from NOTICE"
        assert licence in notice, f"{component} licence not stated in NOTICE"


def test_the_licence_change_is_documented_not_silent():
    """The move from MIT to GPL-3.0 is a permanent, one-way change.

    Someone reading only the README should be able to tell that the project was
    ever MIT, and that old copies stay that way -- otherwise people who
    vendored v0.1.x have no way to learn their terms changed.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GNU General Public License" in readme
    assert "originally MIT-licensed" in readme, (
        "the relicensing and its reason must be stated, not just its result"
    )
    assert "irreversible" in readme or "cannot be withdrawn" in readme


# --- the shipped artefact ----------------------------------------------------


def test_license_and_notice_are_bundled_into_the_release():
    """GPL-3.0 section 4: the licence accompanies the work.

    Checking the repository is not enough. A build that drops these produces a
    non-compliant release from a correctly-licensed source tree.
    """
    spec = (ROOT / "packaging" / "MediaGrab.spec").read_text()
    assert "LICENSE" in spec and "NOTICE" in spec
    assert "GPL-3.0 s4" in spec or "section 4" in spec, (
        "the reason these files are bundled must be recorded next to the code"
    )


def test_ci_fails_when_the_license_is_missing_from_the_bundle():
    """Same reason, enforced where it matters: on the artifact."""
    workflow = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text()
    assert "LICENSE" in workflow and "NOTICE" in workflow

    build_desktop = workflow[workflow.index("  windows:"):]
    assert "GPL-3.0 s4" in build_desktop, (
        "both build jobs must assert the licence is in the bundle"
    )


# --- provenance --------------------------------------------------------------


def test_ffmpeg_provenance_records_the_corresponding_source():
    """GPL-3.0 section 4 obliges the distributor to offer the complete
    corresponding source for the FFmpeg it ships.

    `releases/latest` is a moving tag: the asset name that resolves today
    resolves to a different build tomorrow. Without a recorded commit, the
    answer to "what source is this binary" is unrecoverable a year later.
    """
    fetch = (ROOT / "packaging" / "fetch_ffmpeg.py").read_text()
    assert "PROVENANCE.txt" in fetch
    # Read from the binary, not hardcoded: a hardcoded pin rots silently the
    # moment upstream publishes a new build.
    assert '"-version"' in fetch, "provenance must be read from ffmpeg -version"
    assert "commit" in fetch
    # And it must never be able to break a build.
    assert "must never block a build" in fetch


@pytest.mark.parametrize("archive", ["LICENSE", "NOTICE"])
def test_legal_files_are_not_gitignored(archive):
    """A .gitignore pattern that swallows LICENSE is a silent compliance
    failure: the file exists locally, passes every local check, and is absent
    from the repo everyone clones."""
    ignored = (ROOT / ".gitignore").read_text()
    patterns = [
        line.strip()
        for line in ignored.splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("!")
    ]
    for pattern in patterns:
        if pattern in {archive, f"/{archive}", f"*{archive}"}:
            pytest.fail(f"{archive} is gitignored by pattern {pattern!r}")


def test_prd_no_longer_tracks_licensing_as_a_blocker():
    """The PRD tracked this as an unmet non-functional requirement.

    Left stale, it reads as an outstanding blocker to anyone deciding whether
    the project can be published.
    """
    prd = (ROOT / "PRD.md").read_text(encoding="utf-8")
    assert "GPL-3.0 bundled FFmpeg | Cannot publish" not in prd
    assert "**Open** — blocks distribution" not in prd

    # And the requirement itself must now read as met.
    requirement = [line for line in prd.splitlines() if "| N8 |" in line]
    assert requirement, "N8 requirement line disappeared"
    assert "**Not met**" not in requirement[0], (
        f"N8 still claims the licence obligations are unmet: {requirement[0]}"
    )


def test_every_runtime_dependency_is_gpl3_compatible():
    """Re-licensing is only safe if nothing bundled is GPL-*incompatible*.

    GPL-3.0 cannot absorb a proprietary or GPL-2.0-only licence. The one to
    watch here is mutagen, which is GPL-2.0-*or-later* -- the "or later" clause
    is what permits distribution at version 3, and dropping the project back to
    GPL-2.0-only would break it.
    """
    import importlib.metadata as metadata

    incompatible = {
        "GPL-2.0",  # only -- without "or later" this cannot enter a GPL-3.0 work
        "GPL-2.0-only",
        "GPL-1.0",
        "proprietary",
        "LicenseRef-Proprietary",
    }

    for dist in metadata.distributions():
        expression = (dist.metadata.get("License-Expression") or "").strip()
        if not expression:
            continue  # no SPDX expression means unstated, not incompatible
        name = dist.metadata.get("Name")
        for candidate in expression.split(" OR "):
            if candidate.strip() in incompatible:
                pytest.fail(
                    f"{name} is {candidate.strip()}, which is not compatible "
                    f"with GPL-3.0 (expression: {expression})"
                )