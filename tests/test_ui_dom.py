import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import httpx
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
