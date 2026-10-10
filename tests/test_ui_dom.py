import os
import re
import shutil
import subprocess
import pytest

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

def test_e2e_choose_grab_workflow():
    if not shutil.which("chromium") or not shutil.which("node"):
        pytest.skip("chromium or node not found in PATH")

    # Run the headless Chromium E2E test
    result = subprocess.run(
        ["node", "tests/e2e_choose_grab.mjs"],
        capture_output=True,
        text=True,
        timeout=15
    )
    assert result.returncode == 0, f"Headless E2E test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "SUCCESS" in result.stdout
