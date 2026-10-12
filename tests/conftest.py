"""Session-wide test isolation.

Every test must write downloads somewhere disposable, and the default location
is the repository's own `downloads/`. A desktop-mode download in any test left
a real `.mediagrab-<job_id>/` staging directory in the working tree; that
happened 275 times before this file existed.

The override is an environment variable set at *import* time rather than a
monkeypatch, and it has to be. Roughly a dozen modules do
`from app.config import DOWNLOADS_DIR`, binding the value when they load. A
monkeypatch of `app.config.DOWNLOADS_DIR` reaches none of them -- they keep the
path they captured, and tests then write to the repository anyway while
appearing isolated. Setting the variable before anything imports `app.config`
makes every binding agree on the temporary directory.

Pytest imports conftest.py before any test module, which is early enough.
"""

import atexit
import os
import shutil
import tempfile

_TEST_DOWNLOADS = tempfile.mkdtemp(prefix="mediagrab-test-downloads-")
os.environ["MEDIA_GRAB_DOWNLOADS_DIR"] = _TEST_DOWNLOADS


@atexit.register
def _remove_test_downloads():
    shutil.rmtree(_TEST_DOWNLOADS, ignore_errors=True)


def test_no_test_writes_to_the_repository_downloads_dir():
    """If this fails, a module bypassed the isolation above.

    Guards the mechanism rather than the current outcome: a future
    `from app.config import DOWNLOADS_DIR` in a new module would import
    *after* this file runs and could quietly resolve to the repository again.
    """
    import pathlib

    repo_downloads = pathlib.Path(__file__).resolve().parent.parent / "downloads"
    leaked = sorted(p.name for p in repo_downloads.glob(".mediagrab-*")) if repo_downloads.exists() else []
    assert leaked == [], (
        f"{len(leaked)} staging directories leaked into the repository: {leaked[:5]}"
    )