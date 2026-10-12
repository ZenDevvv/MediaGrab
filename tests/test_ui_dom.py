import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import httpx
from pathlib import Path

import pytest

from app.main import app

def test_dom_ids_integrity():
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    with open("static/choose.html", "r", encoding="utf-8") as f:
        choose_html = f.read()
    with open("static/index.html", "r", encoding="utf-8") as f:
        index_html = f.read()

    # Verify essential interactive IDs exist in static/choose.html
    required_choose_ids = [
        "btn-start-download",
        "btn-choose-back",
        "btn-cancel-download",
        "btn-grab-another",
        "view-choose",
        "view-working",
        "view-ready",
        "view-error",
        "view-offline",
        "live-pct",
        "live-bar-fill",
        "live-clipper",
        "live-speed",
        "live-eta",
        "live-got",
        "working-spec-format",
        "working-spec-clip",
        "ready-filename",
        "ready-spec-format",
        "ready-spec-duration",
        "ready-spec-size",
        "ready-spec-captions",
        "btn-save-media",
        "btn-download-label",
        "config-summary-spec"
    ]
    for element_id in required_choose_ids:
        assert f'id="{element_id}"' in choose_html, f"Missing id='{element_id}' in choose.html"

    # Verify no unguarded references to removed or legacy IDs
    assert "$('#trim-summary-text').textContent" not in js
    assert "$('#error-details-text').textContent" not in js


def test_hosted_ui_elements_present():
    """Milestone 2 surfaces must exist in the markup they are driven from."""
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    with open("static/choose.html", "r", encoding="utf-8") as f:
        choose_html = f.read()
    with open("static/index.html", "r", encoding="utf-8") as f:
        index_html = f.read()

    # Promo strip and its platform links (Decision #3).
    assert 'id="hosted-promo-strip"' in index_html
    for el_id in ("hosted-promo-cta", "hosted-promo-text", "hosted-promo-other",
                  "hosted-promo-panel", "hosted-promo-windows",
                  "hosted-promo-linux", "hosted-promo-android"):
        assert f'id="{el_id}"' in index_html, f"Missing promo element '{el_id}'"

    # Clip-aware guard and the honest retention notice (Decisions #5, #8).
    assert 'id="trim-limit-banner"' in choose_html
    assert 'id="trim-limit-text"' in choose_html
    assert 'id="ready-retention-notice"' in choose_html
    assert 'id="ready-retention-text"' in choose_html
    assert 'id="error-upgrade-hint"' in choose_html

    # The misleading countdown copy must be gone, not merely hidden.
    assert "Deleted from this machine" not in choose_html
    assert "29:59" not in choose_html

    # Behavioural wiring.
    assert "loadCapabilities" in js
    assert "showRateLimited" in js
    assert "isResolutionLocked" in js
    assert "HOSTED_SAFE_DEFAULTS" in js
    assert "resp.status === 429" in js


def test_mode_injection_placeholder_present():
    """The templates must keep the injection point the server replaces."""
    for name in ("index.html", "choose.html"):
        with open(f"static/{name}", "r", encoding="utf-8") as f:
            assert "<!--mg-mode-->" in f.read(), f"Missing mg-mode placeholder in {name}"


def test_meta_mode_injected_per_mode(monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app as fastapi_app

    monkeypatch.setattr(config, "IS_HOSTED", False)
    with TestClient(fastapi_app) as client:
        body = client.get("/").text
        assert '<meta name="mg-mode" content="desktop">' in body

    monkeypatch.setattr(config, "IS_HOSTED", True)
    with TestClient(fastapi_app) as client:
        body = client.get("/").text
        assert '<meta name="mg-mode" content="hosted">' in body
        assert "<!--mg-mode-->" not in body

def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _server(mode: str):
    """Run uvicorn under an explicit MEDIAGRAB_MODE for the duration of a test."""
    port = _free_port()
    env = {**os.environ, "MEDIAGRAB_MODE": mode}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"Server exited during startup (rc={proc.returncode}):\n{output}")
        try:
            if httpx.get(base_url, timeout=1.0).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.2)

    if not ready:
        proc.terminate()
        proc.wait(timeout=10)
        pytest.fail(f"Server did not become ready on {base_url} within timeout")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def live_server():
    """Server for the browser tests, using the ambient (desktop) mode."""
    with _server("desktop") as url:
        yield url


@pytest.fixture(scope="module")
def hosted_server():
    """Server started explicitly in hosted mode for gated-UI assertions."""
    with _server("hosted") as url:
        yield url


def _run_node_script(script: str, base_url: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", script, base_url, str(_free_port()), *extra],
        capture_output=True,
        text=True,
        timeout=120
    )


def test_e2e_choose_grab_workflow(live_server):
    if not shutil.which("chromium") or not shutil.which("node"):
        pytest.skip("chromium or node not found in PATH")

    result = _run_node_script("tests/e2e_choose_grab.mjs", live_server)
    assert result.returncode == 0, (
        f"Headless E2E test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout
    # Guard against a silent regression where the grab button stops posting.
    assert "/api/download" in result.stdout


@pytest.mark.parametrize("mode", ["hosted", "desktop"])
def test_e2e_hosted_ui_gating(mode, request):
    """Hosted gating and its desktop-mode counterpart, in a real browser."""
    if not shutil.which("chromium") or not shutil.which("node"):
        pytest.skip("chromium or node not found in PATH")

    fixture_name = "hosted_server" if mode == "hosted" else "live_server"
    base_url = request.getfixturevalue(fixture_name)
    result = _run_node_script("tests/e2e_hosted_ui.mjs", base_url, mode)

    assert result.returncode == 0, (
        f"Hosted UI E2E ({mode}) failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout


# --- YouTube upsell card (§1.5 Decisions #18-19) --------------------------

@pytest.mark.parametrize("page", ["index.html", "choose.html"])
def test_upsell_card_is_present_and_hidden_by_default(page):
    with open(f"static/{page}", "r", encoding="utf-8") as f:
        html = f.read()
    # Present on both the landing and configure screens, but not shown until
    # the server actually refuses.
    assert html.count('id="yt-upsell"') == 1
    assert 'id="yt-upsell"' in html
    assert 'id="yt-upsell" hidden' in html
    # Actionable: a real CTA, a dismiss affordance, and an id the JS targets.
    assert 'id="yt-upsell-cta"' in html
    assert 'id="yt-upsell-dismiss"' in html
    assert 'id="yt-upsell-text"' in html
    assert 'id="yt-upsell-cta-label"' in html
    # Honest fallback copy, and it points somewhere real rather than "#".
    assert "1,700+ other sites work right now" in html
    assert 'href="/downloads"' in html


def test_upsell_styling_reuses_the_design_system_tokens():
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    block_start = css.index(".yt-upsell {")
    block_end = css.index("/* Main App Container */", block_start)
    block = css[block_start:block_end]

    # --card surface plus a hard edge shadow: a highlight in both themes,
    # unlike .limit-banner's accent-soft fill which reads as disabled in dark.
    assert "var(--card)" in block
    assert "var(--accent)" in block
    assert "var(--edge)" in block
    assert "border-radius" in block
    assert "var(--font-display)" in block
    assert "var(--font-body)" in block
    # Must not collapse when toggled via the hidden attribute.
    assert ".yt-upsell[hidden] { display: none; }" in block


def test_upsell_never_renders_without_a_usable_cta():
    """The dead-end failure mode: a card with no way forward."""
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    start = js.index("function showYouTubeUpsell")
    body = js[start:js.index("function hideYouTubeUpsell", start)]
    # Falls back to the downloads page when no real binary is published yet.
    assert "isRealDownload(url)" in body
    assert "'/downloads'" in body


def test_e2e_youtube_upsell_card(live_server):
    """The blocked-link upsell, in a real browser against a genuine 451 payload."""
    if not shutil.which("chromium") or not shutil.which("node"):
        pytest.skip("chromium or node not found in PATH")

    result = _run_node_script("tests/e2e_youtube_upsell.mjs", live_server)

    assert result.returncode == 0, (
        f"YouTube upsell E2E failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout
    # Guard against the two silent failure modes: a dead CTA, or a card that
    # claims YouTube is fully down when some links still work.
    assert "/api/info" in result.stdout


# --- Ephemeral notice + countdown (§ design-system pass) -------------------

def test_ico_has_intrinsic_size():
    """An SVG with no width/height fills its container.

    The retention clock rendered ~230px wide inside a one-line notice because
    .ico only declared stroke properties.
    """
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    rule = css[css.index(".ico {"):css.index("}", css.index(".ico {"))]
    assert "width:" in rule and "height:" in rule
    assert "flex: none" in rule


def test_retention_notice_has_three_states():
    with open("static/choose.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="ready-retention-notice"' in html
    assert 'id="ready-retention-title"' in html
    assert 'id="ready-retention-text"' in html
    assert 'class="save-notice-ico"' in html

    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    for state in ("is-idle", "is-counting", "is-done"):
        assert f"'{state}'" in js, state


def test_retention_notice_avoids_the_muddy_accent_soft_fill():
    """--accent-soft is a dark olive in dark theme and reads as disabled."""
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    start = css.index(".save-notice {")
    end = css.index("/* The ticking clock", start)
    block = css[start:end]
    assert "background: var(--accent-soft)" not in block
    assert "background: var(--card)" in block


def test_countdown_timer_is_stable_and_legible():
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    start = css.index(".save-notice-timer {")
    block = css[start:css.index("}", start)]
    # Monospaced tabular figures so the pill does not jitter on every tick.
    assert "font-variant-numeric: tabular-nums" in block
    assert "var(--font-mono)" in block
    # On-colour token, not a hardcoded white: dark theme inverts --bad to a
    # light tint where white text would be unreadable.
    assert "var(--on-bad)" in block
    assert "#fff" not in block


def test_on_color_tokens_exist_for_both_themes():
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    assert "--on-bad:" in css
    assert "--on-ok:" in css
    # Defined for the dark theme as well, not just the light default.
    assert css.count("--on-bad:") >= 2
    assert css.count("--on-ok:") >= 2


def test_retention_copy_is_human_readable():
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    # Running prose spells the duration out; the terse form stays in labels.
    assert "retentionPhrase" in js
    assert "deleted from the server" in js
    # The old copy implied the file is deleted before it is saved.
    assert "after you save it. Save it now" not in js


# --- Release publishing is gated on verification (§v0.1.0 regression) ------

def _workflow_text() -> str:
    """Raw file text. yaml.dump re-wraps long lines, breaking substring checks."""
    with open(".github/workflows/build-desktop.yml", "r", encoding="utf-8") as f:
        return f.read()


def _workflow():
    import yaml
    return yaml.safe_load(_workflow_text())


def test_publish_job_waits_for_both_builds():
    """v0.1.0 published a Linux-only release while the Windows job failed."""
    jobs = _workflow()["jobs"]
    assert "publish" in jobs, "a single gated job must create the release"
    assert set(jobs["publish"].get("needs", [])) == {"windows", "linux"}


def test_only_the_gated_job_creates_the_release():
    import yaml

    jobs = _workflow()["jobs"]
    creators = []
    for name, job in jobs.items():
        if "action-gh-release" in yaml.dump(job):
            creators.append(name)
    # If a build job published on its own, a failure in its sibling would
    # still leave a half-release published -- exactly what v0.1.0 did.
    assert creators == ["publish"], creators


def test_windows_verifies_a_no_console_launch():
    """The launch mode that exposes the missing-stdio crash.

    A redirect hands the child real file handles, so its stdout is never None.
    Only an unredirected launch reproduces a double-click.
    """
    windows = _workflow()["jobs"]["windows"]
    step = next(s for s in windows["steps"]
                if s.get("name") == "Smoke test survives a no-console launch")
    body = step["run"]
    assert "Start-Process" in body
    assert "/api/capabilities" in body
    # The regression guard is precisely the absence of redirection.
    assert "-RedirectStandardOutput" not in body
    assert "-RedirectStandardError" not in body


def test_publish_refuses_an_incomplete_release():
    publish = _workflow()["jobs"]["publish"]
    body = "\n".join(s.get("run", "") for s in publish["steps"])
    assert "Refusing to publish an incomplete release" in body
    for asset in ("MediaGrab-Windows.zip", "MediaGrab-Linux-x86_64.tar.gz"):
        assert asset in body


def test_ci_downloads_something_against_the_frozen_binary():
    """v0.1.4 shipped broken because startup tests never download."""
    import yaml

    windows = _workflow()["jobs"]["windows"]
    names = [s.get("name") for s in windows["steps"]]
    assert "Smoke test a real download" in names, (
        "a startup-only smoke test cannot catch post-processing failures"
    )
    step = next(s for s in windows["steps"]
                if s.get("name") == "Smoke test a real download")
    body = step["run"]
    assert "/api/download" in body
    assert "/api/progress" in body
    # Must assert success rather than merely submitting the job.
    assert 'status -ne "ready"' in body


def test_linux_job_also_downloads():
    import yaml

    linux = _workflow()["jobs"]["linux"]
    body = yaml.dump(linux)
    assert "/api/download" in body


def test_ci_download_test_does_not_depend_on_a_third_party_site():
    """GitHub's datacenter IPs trip YouTube's bot check.

    An earlier version of this test downloaded from YouTube and failed for
    reasons unrelated to the code, which makes it useless as a gate.
    """
    import yaml

    jobs = _workflow()["jobs"]
    for name in ("windows", "linux"):
        step = next(s for s in jobs[name]["steps"]
                    if s.get("name") == "Smoke test a real download")
        body = step["run"]
        assert "youtube.com" not in body, f"{name} must not depend on YouTube"
        assert "sample.mp4" in body, f"{name} should use the committed fixture"


def test_download_fixture_is_committed():
    fixture = Path(__file__).resolve().parent / "fixtures" / "sample.mp4"
    assert fixture.exists()
    assert fixture.stat().st_size > 0
    # Small on purpose: CI runs this on every tag.
    assert fixture.stat().st_size < 200_000


# --- Settings panel (§1.7) -------------------------------------------------

@pytest.mark.parametrize("page", ["index.html", "choose.html"])
def test_settings_drawer_markup_is_present_on_both_screens(page):
    with open(f"static/{page}", "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="btn-settings"' in html
    assert 'id="settings-drawer"' in html
    assert 'id="settings-backdrop"' in html
    assert 'id="i-gear"' in html
    # The two capabilities the user asked for.
    assert 'id="btn-open-log"' in html
    assert 'id="btn-copy-log"' in html
    assert 'id="btn-check-updates"' in html


def test_settings_groups_are_hidden_on_hosted_by_default():
    """Diagnostics and update checks are desktop-only; hidden, not dead."""
    with open("static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="settings-log-group" hidden' in html
    assert 'id="settings-update-group" hidden' in html

    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    start = js.index("async function wireSettings")
    body = js[start:js.index("function openSettingsDrawer", start)] if "function openSettingsDrawer" in js[start:] else js[start:start + 6000]
    assert "isHostedMode()" in body
    # Hosted must not show controls its API refuses.
    assert "settings-update-group" in body


def test_settings_drawer_anchors_left_and_cog_is_lower_left():
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    assert ".drawer-panel-left" in css
    start = css.index(".fab-settings {")
    block = css[start:css.index("}", start)]
    assert "left:" in block and "bottom:" in block
    assert "position: fixed" in block
    # Anchored left, since the trigger is on the left.
    left = css[css.index(".drawer-panel-left {"):css.index("}", css.index(".drawer-panel-left {"))]
    assert "left: 0" in left and "translateX(-100%)" in left


def test_copy_log_falls_back_when_clipboard_api_is_unavailable():
    """A packaged app may not have clipboard permission; the text must still land."""
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    start = js.index("const btnCopyLog")
    body = js[start:js.index("if (label && original)", start)]
    assert "navigator.clipboard" in body
    assert "execCommand" in body


def test_build_stamps_the_version_from_the_tag():
    """An update check cannot work if every build reports itself as dev."""
    import yaml

    jobs = _workflow()["jobs"]
    for name in ("windows", "linux"):
        steps = jobs[name]["steps"]
        stamp = next((s for s in steps if s.get("name") == "Stamp version"), None)
        assert stamp is not None, f"{name} does not stamp a version"
        assert "MEDIA_GRAB_VERSION" in stamp["run"]
        assert "GITHUB_REF_NAME" in stamp["run"], "must use the tag being built"
        # It must happen before the build so the constant is baked in.
        names = [s.get("name") for s in steps]
        assert names.index("Stamp version") < names.index("Build")


def test_settings_labels_do_not_uppercase_the_log_path():
    """A path shown in the wrong case is a path you cannot paste back in."""
    with open("static/css/app.css", "r", encoding="utf-8") as f:
        css = f.read()
    assert ".settings-label" in css
    label_rule = css[css.index(".settings-label {"):css.index("}", css.index(".settings-label {"))]
    assert "text-transform: uppercase" in label_rule
    # The blanket rule that caused it must be gone.
    assert ".settings-group .mono {" not in css

    with open("static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert '<span class="settings-label">Version</span>' in html
    assert '<span class="mono">Version</span>' not in html
    # The path and note stay lowercase-capable.
    assert 'id="settings-log-path"' in html
