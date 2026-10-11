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

def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Run a real uvicorn server for the browser to drive.

    The E2E test exercises app.js through actual navigation, so it needs a
    listening server. Previously it assumed one was already running on port
    8000, which meant it only ever passed by accident against a developer's
    stale manual process -- and failed everywhere else.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"Server exited during startup (rc={proc.returncode}):\n{output}")
        try:
            if httpx.get(base_url, timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail(f"Server did not become ready on {base_url} within timeout")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_e2e_choose_grab_workflow(live_server):
    if not shutil.which("chromium") or not shutil.which("node"):
        pytest.skip("chromium or node not found in PATH")

    cdp_port = str(_free_port())

    result = subprocess.run(
        ["node", "tests/e2e_choose_grab.mjs", live_server, cdp_port],
        capture_output=True,
        text=True,
        timeout=90
    )
    assert result.returncode == 0, (
        f"Headless E2E test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "SUCCESS" in result.stdout

    # Guard against a silent regression where the grab button stops posting.
    assert "/api/download" in result.stdout
