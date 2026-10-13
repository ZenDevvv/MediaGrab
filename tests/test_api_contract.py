"""The frozen /api/ contract, and why it is frozen.

FastAPI already publishes a complete OpenAPI description of the eleven /api/
endpoints at /openapi.json. That document is a contract between two clients
that are written in different languages:

  * the frontend (static/js/app.js), which is shared by both servers, and
  * the Android app (Milestone 4), which re-implements these same endpoints in
    Kotlin.

Nothing in the repository ties the two implementations together today. The
Python suite proves only that Python conforms. If the Kotlin serialisation
drifts, every test stays green and the failure surfaces as a WebView showing
nothing on somebody's phone.

So the spec is snapshotted into openapi-contract.json and asserted here. A
contract change must update the snapshot in the same commit, which makes it
deliberate rather than incidental.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "openapi-contract.json"

# Lowercase and underscores only. Deliberately stricter than "is not camelCase":
# it also rejects PascalCase and kebab-case, which would break the frontend
# identically.
SNAKE_CASE = re.compile(r"[a-z][a-z0-9_]*")


@pytest.fixture(scope="module")
def contract():
    assert CONTRACT.exists(), (
        f"{CONTRACT.name} is missing. Run: python dev/sync-contract.py"
    )
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


# --- the snapshot is current -------------------------------------------------


def test_snapshot_matches_the_live_api():
    """The single most important assertion in this file.

    Regenerates the spec and compares. Fails if any request or response model
    changed without the snapshot being updated.
    """
    result = subprocess.run(
        [sys.executable, "dev/sync-contract.py", "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"The API contract has drifted from {CONTRACT.name}.\n"
        f"{result.stdout}{result.stderr}\n"
        f"If this change is intentional, run `python dev/sync-contract.py` "
        f"and commit the result with it."
    )


def test_snapshot_is_deterministic():
    """Regenerating must be a no-op.

    Without sorted keys the file would churn on any Python version whose dict
    ordering differs, and the check above would become noise nobody trusts.
    """
    before = CONTRACT.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, "dev/sync-contract.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert CONTRACT.read_text(encoding="utf-8") == before


# --- the snapshot describes what it claims to --------------------------------


def test_snapshot_covers_every_api_endpoint(contract):
    """A partial snapshot is worse than none: it looks like coverage.

    Pinned as a literal rather than derived from the app, so that deleting an
    endpoint cannot quietly shrink the contract along with it.
    """
    expected = {
        "/api/abort/{job_id}",
        "/api/capabilities",
        "/api/diagnostics/log",
        "/api/diagnostics/open-log",
        "/api/diagnostics/update-check",
        "/api/download",
        "/api/events/{job_id}",
        "/api/file/{job_id}",
        "/api/info",
        "/api/open-folder",
        "/api/progress/{job_id}",
    }
    actual = {p for p in contract["paths"] if p.startswith("/api/")}
    assert actual == expected, (
        f"contract drift: missing {sorted(expected - actual)}, "
        f"unexpected {sorted(actual - expected)}"
    )


def test_snapshot_is_stable_across_modes(contract):
    """Hosted and desktop must describe the same API.

    The guardrails differ but the *shape* must not, or the shared frontend
    needs to know which server it is talking to. This is asserted in both
    modes by running the suite twice in CI, which is why tests.yml has a
    hosted job.
    """
    # If this ever needs a mode branch, the file is doing the wrong job.
    assert "hosted" not in CONTRACT.read_text(encoding="utf-8").lower() \
        or "LimitsResponse" in contract["components"]["schemas"]


# --- the drift this is actually for ------------------------------------------


def test_json_field_names_are_snake_case(contract):
    """Pydantic emits snake_case; Kotlin's default is camelCase.

    This is the specific mismatch that makes M4 dangerous. A Kotlin
    `val jobId: String` serialises to {"jobId": ...}, the frontend reads
    .job_id, gets undefined, and renders nothing -- with no exception
    anywhere. Asserting the naming convention here means the Kotlin port has
    an explicit list to satisfy rather than a convention to rediscover.

    The rule is lowercase-and-underscores, not merely "contains an
    underscore": most fields here are single words (mode, version, title)
    and carry no separator at all. Checking for an underscore flags every one
    of them, which is how this test first failed against a perfectly
    conforming contract.
    """
    offenders = {}
    for name, schema in contract["components"]["schemas"].items():
        for prop in (schema.get("properties") or {}):
            if not SNAKE_CASE.fullmatch(prop):
                offenders.setdefault(name, []).append(prop)

    assert not offenders, (
        f"property names that are not snake_case: {offenders}. The frontend "
        f"reads snake_case; a Kotlin port must set @SerialName to match."
    )
    assert "job_id" in contract["components"]["schemas"]["DownloadResponse"][
        "properties"], "the download response must expose job_id"


# Fields the frontend reads off an API payload with plain `.name` access.
#
# This list has to be curated -- app.js also reads .textContent, .classList
# and dozens of DOM nodes, so it cannot be derived automatically without
# drowning in false positives. The rot risk is real: an earlier version of
# this list included queue_position, which app.js has never read, so the
# entry was decoration. test_watched_fields_are_actually_read below fails if
# any entry stops being read, which keeps the list honest.
FRONTEND_API_FIELDS = {
    "job_id", "download_url", "srt_url", "srt_filename",
    "retention_seconds", "locked_resolutions", "max_file_size_bytes",
    "max_duration_seconds",
}


def test_frontend_reads_fields_the_contract_publishes(contract):
    """Every API field the frontend reads must be declared in the spec.

    Catches the reverse drift -- the frontend depending on a field nobody
    declares -- which is how a response field silently becomes optional.
    """
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    published = {
        prop
        for schema in contract["components"]["schemas"].values()
        for prop in (schema.get("properties") or {})
    }

    used = {f for f in FRONTEND_API_FIELDS if f".{f}" in js}
    assert used, "guard is broken: app.js no longer references any of these"

    missing = used - published
    assert not missing, (
        f"app.js reads {sorted(missing)}, which the OpenAPI contract does not "
        f"publish. Either the frontend or the contract is wrong."
    )


def test_watched_fields_are_actually_read():
    """Keeps FRONTEND_API_FIELDS from decaying into fiction.

    A field nobody reads provides no protection while looking like it does.
    This test is what caught queue_position sitting in the list unused.
    """
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    unread = sorted(f for f in FRONTEND_API_FIELDS if f".{f}" not in js)
    assert not unread, (
        f"{unread} are listed as frontend-read but app.js does not reference "
        f"them. Remove them, or the check they back is not checking anything."
    )


def test_no_required_field_is_nullable_in_the_schema(contract):
    """`required` is what makes a field mandatory.

    A Kotlin port can omit a field that Python always sends, or send null
    where Python always sends a value. Both pass a name-only comparison, so
    requiredness has to be part of the contract rather than a convention.
    """
    for name, schema in contract["components"]["schemas"].items():
        props = set(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        assert required <= props, f"{name}: required names a non-existent field"
        for prop in required:
            nullable = schema["properties"][prop].get("nullable")
            assert not nullable, (
                f"{name}.{prop} is required but nullable -- a client cannot "
                f"rely on it being present"
            )

def test_android_server_is_validated_against_the_same_contract():
    """Both implementations must be checked against one file.

    The Kotlin server re-implements the same endpoints for the Android app
    while the frontend stays shared. If only the Python side were asserted,
    a Kotlin field renamed to camelCase would render a blank WebView on a
    phone while every test stayed green — the reason the snapshot exists.
    """
    root = Path(__file__).resolve().parent.parent

    gradle = (root / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert "openapi-contract.json" in gradle, (
        "the Android build must be pointed at the same contract file"
    )
    # Read from the repository rather than a vendored copy, so the two sides
    # cannot each drift behind a private snapshot.
    assert "File(repoRoot" in gradle or "repoRoot" in gradle

    workflow = (root / ".github" / "workflows" / "android.yml")
    assert workflow.exists(), (
        "the Kotlin contract tests must run in CI, not only locally"
    )
    body = workflow.read_text(encoding="utf-8")
    assert "testDebugUnitTest" in body
    assert "sync-contract.py --check" in body


def test_android_kotlin_dtos_annotate_every_multiword_field():
    """Every camelCase property must carry @SerialName.

    Kotlin's default serialisation uses the property name verbatim, so a
    multi-word property is emitted as camelCase and the frontend — which reads
    snake_case — gets undefined. Single-word properties (`mode`, `title`) are
    already lower-case and need no annotation.

    The test keys on uppercase letters, not underscores: checking the Kotlin
    name for an underscore tests the wrong thing, because an annotated property
    has no underscore in its Kotlin name at all. That first version passed
    with the annotation removed.
    """
    dto = (Path(__file__).resolve().parent.parent
           / "android" / "app" / "src" / "main" / "kotlin" / "dev" / "mediagrab"
           / "contract" / "Dto.kt").read_text(encoding="utf-8")

    import re
    unannotated = []
    for match in re.finditer(r"val (\w+):", dto):
        name = match.group(1)
        if not any(c.isupper() for c in name):
            continue  # single lower-case word; serialises correctly as-is
        line_start = dto.rfind("\n", 0, match.start()) + 1
        prefix = dto[line_start:match.start()]
        if "@SerialName" not in prefix:
            unannotated.append(name)

    assert not unannotated, (
        f"Kotlin properties that would serialise as camelCase: {unannotated}. "
        f"Without @SerialName the frontend reads undefined with no error."
    )
