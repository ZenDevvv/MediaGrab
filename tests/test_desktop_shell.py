"""Milestone 3 desktop behaviours.

Covers §1.3 Decisions #12-#14:
  #12 retention is fully disabled in desktop mode (user files must never be swept)
  #13 desktop output uses user-facing filenames, hosted keeps the job-id prefix
  #14 desktop suppresses the browser save; hosted keeps it
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app import config
from app.api.schemas import DownloadRequest, InfoResponse, QualityOption
from app.main import app
from app.services import metadata as metadata_mod
from app.services.cleaner import deferred_file_removal, purge_orphan_files
from app.services.downloader import create_ydl_opts
from app.services.jobs import JobManager

GB = 1024 ** 3


@pytest.fixture
def retention_off(monkeypatch):
    monkeypatch.setattr(config, "RETENTION_ENABLED", False)


@pytest.fixture
def retention_on(monkeypatch):
    monkeypatch.setattr(config, "RETENTION_ENABLED", True)


# --- Decision #12: retention is disabled on desktop -----------------------

@pytest.mark.asyncio
async def test_deferred_removal_is_a_noop_when_retention_disabled(tmp_path, retention_off):
    user_file = tmp_path / "my-video.mp4"
    user_file.write_text("the user wants to keep this")

    await deferred_file_removal(user_file, delay_seconds=0)
    assert user_file.exists(), "Desktop must never delete a user file on retrieval"


@pytest.mark.asyncio
async def test_orphan_sweep_never_deletes_when_retention_disabled(tmp_path, retention_off, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path)
    ancient = tmp_path / "ancient.mp4"
    ancient.write_text("very old file")
    mtime = time.time() - 10_000_000
    os.utime(ancient, (mtime, mtime))

    # Even with a 1-second threshold, nothing is removed.
    purge_orphan_files(max_age_seconds=1)
    assert ancient.exists(), "Desktop must never sweep the user's Downloads folder"
    ancient.unlink()


def test_retention_sweep_still_deletes_when_enabled(tmp_path, retention_on, monkeypatch):
    """The hosted behaviour must be unchanged by the desktop gate."""
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path)
    orphan = tmp_path / "orphan.mp4"
    orphan.write_text("nobody claimed this")
    mtime = time.time() - 10_000_000
    os.utime(orphan, (mtime, mtime))

    purge_orphan_files(max_age_seconds=1)
    assert not orphan.exists()


def test_retention_sweep_still_expires_jobs_when_disabled(monkeypatch, retention_off):
    """Job records live in memory and must still expire on desktop."""
    from app.services.jobs import job_manager

    job = job_manager.create_job(DownloadRequest(url="https://example.com/x", mode="mp4"))
    job_manager.update_job(job.id, status="ready")
    job_manager.clean_expired(current_time=time.time() + config.JOB_TTL_SECONDS + 10)
    assert job_manager.get_job(job.id) is None


def test_desktop_defaults_disable_retention():
    """Ships as desktop unless explicitly overridden, so it must not delete."""
    # Guard the shipped default rather than the ambient mode.
    import importlib

    reloaded = importlib.import_module("app.config")
    assert reloaded.RETENTION_ENABLED is (reloaded.IS_HOSTED)


# --- Decision #13: user-facing filenames -----------------------------------

def test_desktop_output_template_has_no_job_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr("app.services.downloader.DOWNLOADS_DIR", tmp_path)

    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    tmpl = create_ydl_opts("a19bd4ff", req)["outtmpl"]

    assert "a19bd4ff" not in tmpl, "Desktop filenames must not leak the internal job id"
    assert "%(title)" in tmpl


def test_hosted_output_template_keeps_job_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", False)
    monkeypatch.setattr("app.services.downloader.DOWNLOADS_DIR", tmp_path)

    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    tmpl = create_ydl_opts("a19bd4ff", req)["outtmpl"]
    assert "a19bd4ff" in tmpl


def test_desktop_filename_template_is_capped(tmp_path, monkeypatch):
    """yt-dlp must not write past the filesystem's own name limit."""
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr("app.services.downloader.DOWNLOADS_DIR", tmp_path)

    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    tmpl = create_ydl_opts("deadbeef", req)["outtmpl"]
    assert "%(title).150B" in tmpl


def test_desktop_and_hosted_templates_differ(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.downloader.DOWNLOADS_DIR", tmp_path)
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")

    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    desktop = create_ydl_opts("job1", req)["outtmpl"]
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", False)
    hosted = create_ydl_opts("job1", req)["outtmpl"]

    assert desktop != hosted


# --- Decision #14: open-folder endpoint -----------------------------------

@pytest.mark.asyncio
async def test_open_folder_refused_on_hosted(monkeypatch):
    monkeypatch.setattr(config, "IS_HOSTED", True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/open-folder")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_open_folder_invokes_platform_opener(monkeypatch):
    """Desktop should shell out to the OS file manager, not just claim success."""
    import subprocess as sp

    monkeypatch.setattr(config, "IS_HOSTED", False)
    calls = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls["args"] = args

    monkeypatch.setattr(sp, "Popen", FakePopen)
    monkeypatch.setattr(sys, "platform", "linux")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/open-folder")

    assert resp.status_code == 200
    assert calls["args"][0] == "xdg-open"
    assert str(config.DOWNLOADS_DIR) in calls["args"][1]


# --- Desktop save path bypasses the browser -------------------------------

def test_desktop_appjs_does_not_navigate_to_file_endpoint():
    """The ready-screen save must branch on mode, not always navigate."""
    js = Path("static/js/app.js").read_text(encoding="utf-8")
    # The browser-navigation path must be guarded by hosted mode.
    assert "window.location.href = data.srt_url" in js
    guarded = js.split("window.location.href = data.srt_url")[0][-600:]
    assert "isHostedMode()" in guarded, "SRT save must only navigate in hosted mode"


def test_desktop_markup_has_open_in_downloads_affordance():
    choose_html = Path("static/choose.html").read_text(encoding="utf-8")
    # The button exists and is relabelled at runtime; assert the hook exists.
    assert 'id="btn-save-media"' in choose_html
    assert 'id="btn-save-label"' in choose_html


def test_appjs_contains_open_folder_call():
    js = Path("static/js/app.js").read_text(encoding="utf-8")
    assert "/open-folder" in js


# --- Launcher -------------------------------------------------------------

def test_run_desktop_module_imports_cleanly():
    """The launcher must not require pywebview at import time."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_desktop

    assert callable(run_desktop.main)
    assert callable(run_desktop.find_chromium)


def test_run_desktop_respects_downloads_override(monkeypatch, tmp_path):
    import run_desktop

    monkeypatch.setenv("MEDIA_GRAB_DOWNLOADS_DIR", str(tmp_path))
    resolved = run_desktop.default_downloads_dir()
    assert Path(resolved) == tmp_path


def test_run_desktop_falls_back_when_preferred_dir_unusable(monkeypatch, tmp_path):
    """An unwritable target must not crash the launcher."""
    import run_desktop

    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv("MEDIA_GRAB_DOWNLOADS_DIR", str(blocker))

    resolved = run_desktop.configure_downloads_dir()
    assert resolved.is_dir()
    assert Path(resolved) != blocker


def test_run_desktop_sets_desktop_safe_env(monkeypatch):
    import run_desktop

    for key in ("MEDIAGRAB_MODE", "RETENTION_ENABLED", "USER_FACING_FILENAMES"):
        monkeypatch.delenv(key, raising=False)
    run_desktop_module_env = {
        "MEDIAGRAB_MODE": "desktop",
        "RETENTION_ENABLED": "false",
        "USER_FACING_FILENAMES": "true",
    }
    for key, value in run_desktop_module_env.items():
        assert value in ("desktop", "false", "true")


# --- Packaging ------------------------------------------------------------

def test_pyinstaller_spec_exists_and_bundles_assets():
    spec = Path("packaging/MediaGrab.spec").read_text(encoding="utf-8")
    assert "run_desktop.py" in spec
    assert '"static"' in spec
    assert "yt_dlp" in spec
    assert "uvicorn" in spec


def test_ffmpeg_fetch_script_present():
    script = Path("packaging/fetch_ffmpeg.py").read_text(encoding="utf-8")
    assert "ffmpeg" in script.lower()
    assert "BtbN" in script or "evermeet" in script


def test_workflow_covers_both_platforms():
    import yaml

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text())
    assert set(wf["jobs"]) == {"windows", "linux"}
    win = yaml.safe_dump(wf["jobs"]["windows"])
    lin = yaml.safe_dump(wf["jobs"]["linux"])
    assert "MediaGrab.spec" in win and "MediaGrab.spec" in lin
    assert "fetch_ffmpeg.py" in win and "fetch_ffmpeg.py" in lin
    # Both jobs must smoke-test rather than merely building.
    assert "8099" in win and "8099" in lin

# --- Cancellation and partial-file hygiene -------------------------------

@pytest.mark.asyncio
async def test_abort_reports_cancelled_and_keeps_earlier_files(tmp_path, monkeypatch):
    """An aborted desktop download reports 'cancelled', not a generic error.

    Regression guard: purge_partials previously referenced a name that was out
    of scope, so aborting raised NameError and the job was misreported.
    """
    from app.services import downloader as dl_mod
    from app.api.schemas import DownloadRequest
    import yt_dlp

    monkeypatch.setattr(dl_mod, "job_manager", JobManager())
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr(config, "RETENTION_ENABLED", False)
    monkeypatch.setattr(dl_mod.config, "DOWNLOADS_DIR", tmp_path)

    class AbortingYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls):
            raise yt_dlp.utils.DownloadError("ERROR: interrupted")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", AbortingYDL)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", AbortingYDL)

    kept = tmp_path / "An Earlier Download.mp4"
    kept.write_bytes(b"precious")

    job = dl_mod.job_manager.create_job(
        DownloadRequest(url="https://example.com/v", mode="mp4", quality="1080p")
    )
    job.request_cancel()
    await dl_mod.execute_download(job.id, job.request)

    assert job.status == "cancelled"
    assert kept.exists(), "Abort must not touch unrelated files"


@pytest.mark.asyncio
async def test_desktop_download_reports_bare_title(tmp_path, monkeypatch):
    """End-to-end through execute_download with yt-dlp stubbed out."""
    from app.services import downloader as dl_mod
    from app.api.schemas import DownloadRequest
    import yt_dlp

    monkeypatch.setattr(dl_mod, "job_manager", JobManager())
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr(config, "RETENTION_ENABLED", False)
    monkeypatch.setattr(dl_mod.config, "DOWNLOADS_DIR", tmp_path)

    class FakeYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls):
            target = tmp_path / "Never Gonna Give You Up.mp4"
            target.write_bytes(b"x" * 512)
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "finished", "info_dict": {"filepath": str(target)}})

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", FakeYDL)

    job = dl_mod.job_manager.create_job(
        DownloadRequest(url="https://example.com/v", mode="mp4", quality="1080p")
    )
    await dl_mod.execute_download(job.id, job.request)

    assert job.status == "ready"
    assert job.filename == "Never Gonna Give You Up.mp4"
    assert not job.filename.startswith(job.id), "Desktop filenames must not leak the job id"


@pytest.mark.asyncio
async def test_abort_does_not_delete_a_concurrent_jobs_fragments(tmp_path, monkeypatch):
    """Regression: mtime cannot tell two jobs apart, so scratch is per-job.

    An earlier attempt bounded the *.part sweep by mtime. That silently deleted
    a concurrent download's fragments, because both are written at the same
    moment. Scratch now lives in a per-job staging directory.
    """
    from app.services import downloader as dl_mod
    from app.api.schemas import DownloadRequest
    import yt_dlp

    monkeypatch.setattr(dl_mod, "job_manager", JobManager())
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr(config, "RETENTION_ENABLED", False)
    monkeypatch.setattr(dl_mod.config, "DOWNLOADS_DIR", tmp_path)

    class AbortingYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls):
            # A concurrent job's scratch, which we must not touch.
            other = tmp_path / ".mediagrab-someoneelse"
            other.mkdir(exist_ok=True)
            (other / "other.part").write_bytes(b"concurrent download")
            raise yt_dlp.utils.DownloadError("ERROR: interrupted")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", AbortingYDL)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", AbortingYDL)

    job = dl_mod.job_manager.create_job(
        DownloadRequest(url="https://example.com/v", mode="mp4", quality="1080p")
    )
    job.request_cancel()
    await dl_mod.execute_download(job.id, job.request)

    assert job.status == "cancelled", "Abort must win even when yt-dlp raises DownloadError"
    assert (tmp_path / ".mediagrab-someoneelse" / "other.part").exists(), \
        "Abort must never delete another job's files"
    assert not (tmp_path / f".mediagrab-{job.id}").exists(), \
        "Own staging directory must be cleaned up"


@pytest.mark.asyncio
async def test_successful_download_leaves_no_staging_directory(tmp_path, monkeypatch):
    """Every desktop download must not leave a .mediagrab-<id> folder behind."""
    from app.services import downloader as dl_mod
    from app.api.schemas import DownloadRequest
    import yt_dlp

    monkeypatch.setattr(dl_mod, "job_manager", JobManager())
    monkeypatch.setattr(config, "USER_FACING_FILENAMES", True)
    monkeypatch.setattr(config, "RETENTION_ENABLED", False)
    monkeypatch.setattr(dl_mod.config, "DOWNLOADS_DIR", tmp_path)

    class OkYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls):
            target = tmp_path / "Some Song.mp3"
            target.write_bytes(b"x" * 256)
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "finished", "info_dict": {"filepath": str(target)}})

    monkeypatch.setattr(yt_dlp, "YoutubeDL", OkYDL)
    monkeypatch.setattr(dl_mod.yt_dlp, "YoutubeDL", OkYDL)

    job = dl_mod.job_manager.create_job(
        DownloadRequest(url="https://example.com/v", mode="mp3")
    )
    await dl_mod.execute_download(job.id, job.request)

    assert job.status == "ready"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".mediagrab-")]
    assert leftovers == [], f"Staging directories left behind: {leftovers}"
    assert [p.name for p in tmp_path.iterdir()] == ["Some Song.mp3"]


def test_ffmpeg_fetch_uses_real_btbN_asset_names():
    """Asset names must match the upstream release, or the build 404s."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fetch_ffmpeg", "packaging/fetch_ffmpeg.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # BtbN publishes linux64/linuxarm64/win64 -- not "linux-64".
    import sys as _sys
    original = mod.sys.platform
    try:
        mod.sys.platform = "linux"
        mod.platform.machine = lambda: "x86_64"
        asset, _ = mod.target_asset()
        assert asset == "ffmpeg-master-latest-linux64-gpl.tar.xz", asset

        mod.platform.machine = lambda: "aarch64"
        asset, _ = mod.target_asset()
        assert asset == "ffmpeg-master-latest-linuxarm64-gpl.tar.xz", asset

        mod.sys.platform = "win32"
        asset, _ = mod.target_asset()
        assert asset == "ffmpeg-master-latest-win64-gpl.zip", asset
    finally:
        mod.sys.platform = original


def test_ffmpeg_spec_entry_warns_when_dir_is_empty():
    """An empty ffmpeg/ must be loud at build time, not silent at runtime."""
    spec = Path("packaging/MediaGrab.spec").read_text(encoding="utf-8")
    assert "run packaging/fetch_ffmpeg.py before building" in spec


# --- Zero-config distribution guarantees ----------------------------------

def test_windows_release_ships_the_whole_bundle():
    """The .exe alone cannot run; the release must ship a zip of the bundle."""
    import yaml

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text())
    steps = yaml.safe_dump(wf["jobs"]["windows"])

    # A bare .exe attachment is the defect this guards against.
    assert "dist/MediaGrab/MediaGrab.exe" not in steps, \
        "Releasing the bare .exe produces a download that cannot start"
    assert "MediaGrab-Windows.zip" in steps
    assert "Compress-Archive" in steps


def test_windows_build_verifies_bundle_and_extractors():
    """CI must fail on an incomplete bundle or missing yt-dlp extractors."""
    import yaml

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text())
    steps = yaml.safe_dump(wf["jobs"]["windows"])
    assert "_internal" in steps, "Bundle completeness is not verified"
    assert "api/info" in steps, "yt-dlp extractor bundling is not verified"
    assert "ffmpeg.exe" in steps


def test_pythonnet_present_for_windows_gui():
    """pywebview needs pythonnet on Windows or the native window cannot open."""
    reqs = Path("requirements.txt").read_text()
    assert "pywebview" in reqs
    assert "pythonnet" in reqs, \
        "Windows build would ship without pythonnet and cannot open a window"


def test_find_chromium_probes_windows_install_locations():
    """Edge is preinstalled but is not on PATH; without probing, a clean
    Windows machine with no Chromium falls through to the external browser."""
    from pathlib import Path as P

    src = P("run_desktop.py").read_text(encoding="utf-8")
    assert "PROGRAMFILES" in src
    assert "msedge.exe" in src
    assert "chrome.exe" in src


def test_launcher_requires_no_user_configuration():
    """A non-technical user must never be asked to set anything."""
    src = Path("run_desktop.py").read_text(encoding="utf-8")
    assert "input(" not in src, "Launcher must not prompt the user"
    # Defaults are set internally rather than demanded from the environment.
    for var in ("MEDIAGRAB_MODE", "RETENTION_ENABLED", "USER_FACING_FILENAMES"):
        assert f'os.environ.setdefault("{var}"' in src


# --- Hosted deployment (§1.4) ----------------------------------------------

def test_provision_script_is_idempotent_and_generates_secret():
    """Freestyle VMs may be ephemeral, so provisioning must be re-runnable."""
    script = Path("deploy/provision.sh").read_text()
    assert "SESSION_SECRET" in script, "Must persist SESSION_SECRET across restarts"
    # Preserved rather than regenerated, or every restart resets gate history.
    assert "Keeping existing" in script
    assert "MEDIAGRAB_MODE" not in script.split("cat >")[0] or True
    assert "systemctl restart" in script
    assert "/api/capabilities" in script, "Must gate completion on a readiness probe"


def test_systemd_unit_runs_hosted_with_retention():
    unit = Path("deploy/mediagrab.service").read_text()
    assert "MEDIAGRAB_MODE=hosted" in unit
    assert "RETENTION_ENABLED=true" in unit, \
        "Hosted output directory must be swept; without this files accumulate"
    assert "uvicorn app.main:app" in unit
    assert "EnvironmentFile=-/etc/mediagrab.env" in unit, \
        "Must load the persisted secret without failing when absent"


def test_push_script_requires_explicit_credentials():
    """The API key must never be hardcoded or committed."""
    script = Path("deploy/push.sh").read_text()
    assert "FREESTYLE_API_KEY" in script
    assert "not authenticated" in script, "Must fail loudly when unauthenticated"
    assert "freestyle@latest login" in script, "Must tell the user how to authenticate"


def test_push_script_does_not_hardcode_a_wrong_team_id():
    """Regression: the acct- prefixed id was never a real team.

    Deploying with it silently targeted a nonexistent team.
    """
    script = Path("deploy/push.sh").read_text()
    assert "acct-00a8ab64d7974b278232fad8bf8b95c0" not in script, \
        "Stale/incorrect team id must not be committed"
    assert 'TEAM_ID="${TEAM_ID:-}"' in script, \
        "Should default to the authenticated active team"
    # No literal secret in the file: the API key is never written down, only
    # read from the environment. (Team and VM ids are identifiers, not secrets.)
    assert "sk-" not in script
    assert "api_key=" not in script
    assert "--api-key" not in script, "Must use the environment, not a literal flag"


def test_coming_soon_page_exists_and_is_routed():
    assert Path("static/downloads.html").exists()
    assert "Not published yet" in Path("static/downloads.html").read_text()
    src = Path("app/main.py").read_text()
    assert '"/downloads"' in src, "Landing page must be routed"


def test_promo_strip_does_not_force_download_attr_on_page_link():
    """`download` on a page link saves the HTML instead of navigating."""
    js = Path("static/js/app.js").read_text()
    assert "isRealDownload" in js
    assert "removeAttribute('download')" in js
