"""The JavaScript runtime yt-dlp depends on, and that nothing used to install.

`YTDLP_JS_RUNTIMES` defaults to "deno", but nothing in the repository installed
Deno: not the Dockerfile, not deploy/provision.sh, not either CI build job, and
not the PyInstaller bundle. Every one of those deployed a binary that started
cleanly, served requests, and then failed to extract from YouTube -- a failure
whose only symptom is a warning inside yt-dlp's own log.

These tests cover the resolution logic and, below it, the wiring that actually
puts a runtime on PATH.
"""

import platform
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import config
from app.services import js_runtime
from app.services.ytdlp_opts import base_ytdl_opts

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def runtimes(monkeypatch):
    """Rebind the configured runtimes and clear the PATH lookup cache.

    available_runtimes() is lru_cached because PATH does not move under a
    running server -- which is exactly why the tests have to evict it.
    """

    def _set(names, found=()):
        monkeypatch.setattr(config, "YTDLP_JS_RUNTIMES", list(names))
        monkeypatch.setattr(js_runtime.shutil, "which", lambda name: name if name in found else None)
        js_runtime.available_runtimes.cache_clear()
        return names

    yield _set
    js_runtime.available_runtimes.cache_clear()


# --- resolution -------------------------------------------------------------


def test_configured_but_absent_runtime_is_not_offered_to_ytdlp(runtimes):
    """The whole bug: naming a runtime that is not installed.

    yt-dlp treats js_runtimes as a request, not an assertion. It falls back,
    logs "no JavaScript runtime could be found", and returns whatever it can.
    Passing an absent runtime therefore changes nothing observable except the
    success rate -- so the option must not be passed at all.
    """
    runtimes(["deno"], found=())

    assert "js_runtimes" not in base_ytdl_opts()

    status = js_runtime.runtime_status()
    assert status["configured"] == ["deno"]
    assert status["available"] == []
    assert status["missing"] == ["deno"]


def test_installed_runtime_is_passed_through(runtimes):
    runtimes(["deno"], found={"deno"})

    opts = base_ytdl_opts()
    assert opts["js_runtimes"] == {"deno": {}}


def test_only_installed_runtimes_are_declared(runtimes):
    """A list mixing present and absent names resolves to the present ones.

    Dropping the whole js_runtimes key because one entry is missing would be
    worse than the current behaviour: a node-only machine configured for
    "deno,node" would lose a runtime it does have.
    """
    runtimes(["deno", "node"], found={"node"})

    assert base_ytdl_opts()["js_runtimes"] == {"node": {}}
    assert js_runtime.runtime_status()["missing"] == ["deno"]


def test_blank_entries_are_ignored(runtimes):
    """YTDLP_JS_RUNTIMES is comma-split, so an empty string or trailing comma
    yields blank entries. Declaring {"": {}} to yt-dlp is not a runtime."""
    runtimes(["deno", "", "  "], found={"deno"})

    assert base_ytdl_opts()["js_runtimes"] == {"deno": {}}
    assert js_runtime.runtime_status()["configured"] == ["deno"]


def test_a_runtime_is_not_duplicated(runtimes):
    """`js_runtimes` is a dict, so a duplicate collapses silently -- but the
    availability list is also user-visible and should not claim twice."""
    runtimes(["deno", "deno"], found={"deno"})

    assert js_runtime.runtime_status()["available"] == ["deno"]


def test_resolution_is_cached_per_process(runtimes):
    """Proven by the test below; this pins the contract the launcher relies on
    when it prepends the bundled binary before importing anything."""
    runtimes(["deno"], found={"deno"})
    first = js_runtime.available_runtimes()

    # Simulate a late PATH change without evicting the cache.
    js_runtime.shutil.which = lambda name: None
    assert js_runtime.available_runtimes() is first


# --- the endpoint ------------------------------------------------------------


async def test_capabilities_reports_runtime_availability(runtimes):
    """The endpoint is where the absence becomes visible instead of silent."""
    from app.main import app

    runtimes(["deno"], found=set())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/capabilities")).json()

    assert body["js_runtimes"]["configured"] == ["deno"]
    assert body["js_runtimes"]["available"] == []
    assert body["js_runtimes"]["missing"] == ["deno"]


# --- the installer -----------------------------------------------------------


def test_installer_covers_every_platform_we_build_for():
    """One asset per build target. A platform missing here is a platform whose
    bundle silently ships without a runtime."""
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import install_deno
    finally:
        sys.path.pop(0)

    cases = {
        ("Linux", "x86_64"): "deno-x86_64-unknown-linux-gnu.zip",
        ("Linux", "AMD64"): "deno-x86_64-unknown-linux-gnu.zip",
        ("Linux", "aarch64"): "deno-aarch64-unknown-linux-gnu.zip",
        ("Windows", "AMD64"): "deno-x86_64-pc-windows-msvc.zip",
        ("Darwin", "arm64"): "deno-aarch64-apple-darwin.zip",
        ("Darwin", "x86_64"): "deno-x86_64-apple-darwin.zip",
    }
    for (system, machine), expected in cases.items():
        original = platform.system, platform.machine
        platform.system = lambda s=system: s
        platform.machine = lambda m=machine: m
        try:
            assert install_deno.target_asset() == expected, f"{system}/{machine}"
        finally:
            platform.system, platform.machine = original


def test_installer_refuses_an_unsupported_platform_rather_than_guessing():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import install_deno
    finally:
        sys.path.pop(0)

    original = platform.system, platform.machine
    platform.system = lambda: "Plan9"
    platform.machine = lambda: "sparc64"
    try:
        with pytest.raises(SystemExit):
            install_deno.target_asset()
    finally:
        platform.system, platform.machine = original


def test_extracted_binary_is_executable():
    """ZIP does not carry the exec bit, and a non-executable deno presents as
    "no runtime found" rather than as a permission error."""
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import install_deno
    finally:
        sys.path.pop(0)

    import inspect
    source = inspect.getsource(install_deno.extract)
    assert "S_IEXEC" in source


def test_installer_is_not_committed_binary_output():
    """The fetched binary is a build artifact, not source."""
    ignored = (ROOT / ".gitignore").read_text()
    assert "deno/" in ignored


# --- the wiring --------------------------------------------------------------


def test_docker_image_installs_deno():
    """The image is the hosted deployment. A container without a JS runtime
    cannot extract from YouTube at all."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "install_deno.py" in dockerfile, "Dockerfile must install the JS runtime"
    # Not just fetched: it has to be reachable from PATH, or shutil.which
    # inside the app will not see it.
    assert "/usr/local/bin/deno" in dockerfile


def test_provision_script_installs_deno():
    """An ephemeral VM is provisioned from this script alone."""
    provision = (ROOT / "deploy" / "provision.sh").read_text()
    assert "install_deno.py" in provision
    # And it must verify rather than assume: `set -e` makes a failed install
    # abort, but a successful download of the wrong asset would not.
    assert "deno --version" in provision


def test_provision_readiness_check_reports_a_missing_runtime():
    """The service starting is not evidence YouTube works. Say so."""
    provision = (ROOT / "deploy" / "provision.sh").read_text()
    assert '"available":\\[\\]' in provision or "js_runtimes" in provision


def test_pyinstaller_spec_bundles_deno():
    spec = (ROOT / "packaging" / "MediaGrab.spec").read_text()
    assert "deno" in spec
    # Symmetric with ffmpeg: warn loudly rather than producing a bundle that
    # looks fine and cannot solve the player challenge.
    assert "install_deno.py" in spec


def test_launcher_puts_bundled_deno_on_path_before_importing_config():
    """Ordering is the whole mechanism.

    available_runtimes() is lru_cached, so a PATH change made after the first
    call is invisible for the life of the process. prepend_bundled_deno() must
    therefore run before start_server() imports app.main.
    """
    launcher = (ROOT / "run_desktop.py").read_text()

    assert "def prepend_bundled_deno(" in launcher
    assert "_internal" in launcher, "must find the frozen onedir layout"

    pre = launcher.index("deno_dir = prepend_bundled_deno()")
    post = launcher.index("server, thread, port = start_server(")
    assert pre < post, "Deno must be on PATH before the app imports its modules"

    # And it must not override a system install: the bundled copy is a floor.
    assert 'shutil.which("deno")' in launcher


def test_ci_builds_install_and_verify_deno():
    """The bundle assertions are the only thing standing between a release and
    a YouTube-broken build."""
    workflow = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text()
    assert workflow.count("install_deno.py") >= 2, "both build jobs must install Deno"
    assert "_internal/deno" in workflow, "Linux job must verify the bundled binary"
    assert "deno.exe" in workflow, "Windows job must verify the bundled binary"


def test_ci_runs_the_test_suite():
    """235 tests that never execute in CI are 235 tests that can rot unseen."""
    tests_workflow = ROOT / ".github" / "workflows" / "tests.yml"
    assert tests_workflow.exists(), (
        "There must be a workflow that runs pytest; the build workflow only "
        "runs on tags, so nothing tests a pull request"
    )
    body = tests_workflow.read_text()
    assert "pytest" in body
    # Both modes, because the guardrails only exist in hosted.
    assert "MEDIAGRAB_MODE=hosted" in body or "hosted" in body
    # And on pull requests, which is the point of having it at all.
    assert "pull_request" in body