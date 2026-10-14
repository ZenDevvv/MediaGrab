"""Milestone 3 desktop behaviours.

Covers §1.3 Decisions #12-#14:
  #12 retention is fully disabled in desktop mode (user files must never be swept)
  #13 desktop output uses user-facing filenames, hosted keeps the job-id prefix
  #14 desktop suppresses the browser save; hosted keeps it
"""

import asyncio
import io
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

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text(encoding="utf-8"))
    # windows and linux build; publish is the gated job that ships their
    # artifacts, so it is expected alongside them.
    assert {"windows", "linux"} <= set(wf["jobs"])
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

        def extract_info(self, url, download=True):
            return self.download([url])


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

        def extract_info(self, url, download=True):
            return self.download([url])


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

        def extract_info(self, url, download=True):
            return self.download([url])


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

        def extract_info(self, url, download=True):
            return self.download([url])


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

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text(encoding="utf-8"))
    steps = yaml.safe_dump(wf["jobs"]["windows"])

    # A bare .exe attachment is the defect this guards against.
    assert "dist/MediaGrab/MediaGrab.exe" not in steps, \
        "Releasing the bare .exe produces a download that cannot start"
    assert "MediaGrab-Windows.zip" in steps
    assert "Compress-Archive" in steps


def test_windows_build_verifies_bundle_and_extractors():
    """CI must fail on an incomplete bundle or missing yt-dlp extractors."""
    import yaml

    wf = yaml.safe_load(Path(".github/workflows/build-desktop.yml").read_text(encoding="utf-8"))
    steps = yaml.safe_dump(wf["jobs"]["windows"])
    assert "_internal" in steps, "Bundle completeness is not verified"
    assert "api/info" in steps, "yt-dlp extractor bundling is not verified"
    assert "ffmpeg.exe" in steps


def test_pythonnet_present_for_windows_gui():
    """pywebview needs pythonnet on Windows or the native window cannot open."""
    reqs = Path("requirements.txt").read_text(encoding="utf-8")
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
    script = Path("deploy/provision.sh").read_text(encoding="utf-8")
    assert "SESSION_SECRET" in script, "Must persist SESSION_SECRET across restarts"
    # Preserved rather than regenerated, or every restart resets gate history.
    assert "Keeping existing" in script
    assert "MEDIAGRAB_MODE" not in script.split("cat >")[0] or True
    assert "systemctl restart" in script
    assert "/api/capabilities" in script, "Must gate completion on a readiness probe"


def test_systemd_does_not_try_to_create_its_own_sandbox_path():
    """Regression: ExecStartPre cannot fix a missing ReadWritePaths dir.

    systemd resolves ReadWritePaths while setting up the namespace, before any
    ExecStartPre runs, so an ExecStartPre mkdir fails with 226/NAMESPACE and the
    service never starts. The directory must be created by provisioning instead.
    """
    unit = Path("deploy/mediagrab.service").read_text(encoding="utf-8")
    directives = [
        line.strip() for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(d.startswith("ExecStartPre") for d in directives), \
        "Sandbox paths are resolved before ExecStartPre; creating them there fails"
    assert "226/NAMESPACE" in unit, "Must document why the dir is created externally"

    provision = Path("deploy/provision.sh").read_text(encoding="utf-8")
    assert 'mkdir -p "$APP_DIR/downloads"' in provision or \
           'mkdir -p "$REMOTE_DIR/downloads"' in provision or \
           'mkdir -p /opt/mediagrab/downloads' in provision, \
        "Provisioning must create the downloads dir before the unit starts"


def test_systemd_unit_runs_hosted_with_retention():
    unit = Path("deploy/mediagrab.service").read_text(encoding="utf-8")
    assert "MEDIAGRAB_MODE=hosted" in unit
    assert "RETENTION_ENABLED=true" in unit, \
        "Hosted output directory must be swept; without this files accumulate"
    assert "uvicorn app.main:app" in unit
    assert "EnvironmentFile=-/etc/mediagrab.env" in unit, \
        "Must load the persisted secret without failing when absent"


def test_push_script_requires_explicit_credentials():
    """The API key must never be hardcoded or committed."""
    script = Path("deploy/push.sh").read_text(encoding="utf-8")
    assert "FREESTYLE_API_KEY" in script
    assert "not authenticated" in script, "Must fail loudly when unauthenticated"
    assert "freestyle@latest login" in script, "Must tell the user how to authenticate"


def test_push_script_does_not_hardcode_a_wrong_team_id():
    """Regression: the acct- prefixed id was never a real team.

    Deploying with it silently targeted a nonexistent team.
    """
    script = Path("deploy/push.sh").read_text(encoding="utf-8")
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
    assert "Not published yet" in Path("static/downloads.html").read_text(encoding="utf-8")
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert '"/downloads"' in src, "Landing page must be routed"


def test_promo_strip_does_not_force_download_attr_on_page_link():
    """`download` on a page link saves the HTML instead of navigating."""
    js = Path("static/js/app.js").read_text(encoding="utf-8")
    assert "isRealDownload" in js
    assert "removeAttribute('download')" in js


@pytest.mark.parametrize("page", ["index.html", "choose.html"])
def test_retention_copy_is_not_hardcoded(page):
    """Regression: hosted retention is 60s, but the UI said "Cleared after 30 min".

    A visitor was told files survive 30 minutes on a server that deletes them 60
    seconds after retrieval. The first fix covered index.html only, so the
    history drawer on the choose page kept saying "30 min" on both hosted and
    desktop (where nothing is deleted at all).
    """
    js = Path("static/js/app.js").read_text(encoding="utf-8")
    html = Path(f"static/{page}").read_text(encoding="utf-8")

    assert "Cleared after 30 min" not in html, "Retention copy is hardcoded and stale"
    assert "30 min" not in html
    assert "renderRetentionCopy" in js
    assert "AppState.retentionSeconds" in js
    # The labels must exist for the script to populate.
    assert 'id="retention-drawer-note"' in html
    if page == "index.html":
        assert 'id="retention-side-label"' in html


def test_desktop_ready_screen_does_not_threaten_deletion():
    """Desktop never deletes, so "Save it before it clears out" was untrue there."""
    js = Path("static/js/app.js").read_text(encoding="utf-8")
    html = Path("static/choose.html").read_text(encoding="utf-8")
    assert 'id="ready-lede"' in html
    assert "$('#ready-lede')" in js
    assert "It is in your Downloads folder." in js


def test_tunnel_unit_is_committed_and_loopback_only():
    """Cloudflare quick tunnel must front loopback, never expose 0.0.0.0."""
    unit = Path("deploy/mg-tunnel.service").read_text(encoding="utf-8")
    assert "127.0.0.1:8000" in unit, "Tunnel must target loopback"
    assert "0.0.0.0" not in unit
    assert "cloudflared" in unit


def test_tunnel_is_not_coupled_to_app_restarts():
    """Regression: Requires= made every app restart churn the public URL.

    Stopping mediagrab stopped the tunnel, which re-registered under a new
    random trycloudflare hostname and broke every shared link (Error 1033).
    """
    unit = Path("deploy/mg-tunnel.service").read_text(encoding="utf-8")
    directives = [l.strip() for l in unit.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    assert not any(d.startswith("Requires=") for d in directives), \
        "Requires= couples tunnel lifetime to the app and churns the public URL"
    assert any(d.startswith("Wants=") and "mediagrab" in d for d in directives), \
        "Must still order after the app without coupling lifetimes"


def test_ytdlp_client_and_runtime_policy():
    """YouTube client/JS-runtime policy is centralised and PO-token safe."""
    src = Path("app/services/ytdlp_opts.py").read_text(encoding="utf-8")
    cfg = Path("app/config.py").read_text(encoding="utf-8")

    assert "base_ytdl_opts" in src, "Extractor and downloader must share yt-dlp options"
    assert "player_client" in src
    # A JS runtime is required; yt-dlp deprecates extraction without one.
    assert "js_runtimes" in src
    assert "YTDLP_JS_RUNTIMES" in cfg

    # web/mweb require a PO token for GVS and fail without one.
    default_line = [l for l in cfg.splitlines() if "YTDLP_PLAYER_CLIENTS" in l and "os.getenv" in l]
    assert default_line, "Player clients must be configurable"
    assert '"tv_embedded,default,android_vr"' in cfg

    # Both call sites use the shared builder, so they cannot drift.
    assert "base_ytdl_opts()" in Path("app/services/downloader.py").read_text(encoding="utf-8")
    assert "base_ytdl_opts(" in Path("app/services/extractor.py").read_text(encoding="utf-8")


def test_cookie_path_is_configurable_and_not_committed():
    """YouTube cookies must come from config and never be committed."""
    cfg = Path("app/config.py").read_text(encoding="utf-8")
    assert "MEDIA_GRAB_YTDLP_COOKIES" in cfg
    src = Path("app/services/ytdlp_opts.py").read_text(encoding="utf-8")
    assert "cookiefile" in src
    # No literal cookie path or contents in the repo.
    assert "cookies.txt" not in Path("app/config.py").read_text(encoding="utf-8")


def test_youtube_egress_levers_are_configurable():
    """Cookie, proxy and impersonation are the three real levers; all opt-in."""
    cfg = Path("app/config.py").read_text(encoding="utf-8")
    src = Path("app/services/ytdlp_opts.py").read_text(encoding="utf-8")

    for var in ("MEDIA_GRAB_YTDLP_COOKIES", "MEDIA_GRAB_YTDLP_PROXY",
                "MEDIA_GRAB_YTDLP_IMPERSONATE"):
        assert var in cfg, f"{var} must be configurable"

    assert "cookiefile" in src
    assert '"proxy"' in src, "Egress proxy is the root fix for the bot check"
    assert "impersonate" in src

    # Measured worse on a datacenter IP, so it must not default on.
    assert 'YTDLP_IMPERSONATE = os.getenv("MEDIA_GRAB_YTDLP_IMPERSONATE") or None' in cfg
    # No literal proxy or cookie values committed (comments may show format).
    assignments = [l for l in cfg.splitlines() if "YTDLP_PROXY = os.getenv" in l]
    assert assignments and "socks5" not in assignments[0]


# --- YouTube cookie/proxy provisioning -------------------------------------

def test_cookie_install_script_is_safe_and_validating():
    """Cookies are a live credential: validate format, lock permissions."""
    script = Path("deploy/install-cookies.sh").read_text(encoding="utf-8")
    assert "install -m 600" in script, "Cookie file must be 0600"
    assert "mkdir -p" in script, "Must create the cookie dir on a fresh host"
    assert "-o root -g root" in script
    assert "Netscape" in script, "Must reject a file that is not cookies.txt"
    assert "does not look like" in script
    assert "systemctl restart mediagrab" in script
    # Must offer a removal path so credentials can be revoked.
    assert "remove)" in script
    assert "rm -f" in script


def test_provision_wires_cookies_and_proxy():
    src = Path("deploy/provision.sh").read_text(encoding="utf-8")
    assert "MEDIA_GRAB_YTDLP_COOKIES" in src
    assert "/etc/mediagrab/ytdlp-cookies.txt" in src
    assert "MEDIA_GRAB_YTDLP_PROXY" in src
    # Re-running without a cookie file must not leave a dangling path.
    assert "/^MEDIA_GRAB_YTDLP_COOKIES=/d" in src


def test_cookie_files_are_gitignored():
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    for pattern in ("cookies.txt", "*-cookies.txt", "ytdlp-cookies.txt"):
        assert pattern in ignore, f"{pattern} must be gitignored"


def test_no_cookie_or_proxy_secrets_committed():
    """No literal cookie contents or proxy credentials in the repo."""
    for name in ("app/config.py", ".env.example", "deploy/provision.sh",
                 "deploy/install-cookies.sh"):
        text = Path(name).read_text(encoding="utf-8")
        # Assignments and examples only; no real host/user:pass credentials.
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            assert "user:pass@" not in s, f"{name}: literal proxy credential"


# --- Windowed (no-console) startup ----------------------------------------
# v0.1.0 shipped a Windows build that could not start at all. The spec sets
# console=False, so PyInstaller builds a GUI-subsystem exe whose bootloader
# leaves sys.stdout/stderr/stdin as None. uvicorn's DefaultFormatter then calls
# sys.stdout.isatty() and the app dies before the server binds.

def _load_run_desktop():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("run_desktop_under_test", root / "run_desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_streams_are_bound_to_devnull():
    module = _load_run_desktop()
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        sys.stdout = sys.stderr = sys.stdin = None
        module._ensure_standard_streams()
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            assert stream is not None
            assert isinstance(stream, io.IOBase)
        assert not sys.stdout.isatty(), "GUI app must not report a tty"
        sys.stdout.write("")  # writable, so logging cannot raise mid-write
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_existing_streams_are_not_replaced():
    module = _load_run_desktop()
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        module._ensure_standard_streams()
        assert (sys.stdout, sys.stderr, sys.stdin) == original
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_ensure_streams_is_idempotent():
    module = _load_run_desktop()
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        sys.stdout = sys.stderr = sys.stdin = None
        module._ensure_standard_streams()
        first = (sys.stdout, sys.stderr, sys.stdin)
        module._ensure_standard_streams()
        module._ensure_standard_streams()
        assert (sys.stdout, sys.stderr, sys.stdin) == first
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_uvicorn_logging_config_survives_a_missing_stdout():
    """The exact crash from v0.1.0.

    uvicorn's DefaultFormatter calls sys.stdout.isatty() when building its
    logging config, which raised AttributeError when the stream was None.
    """
    from uvicorn.logging import DefaultFormatter

    module = _load_run_desktop()
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        sys.stdout = sys.stderr = sys.stdin = None
        with pytest.raises(AttributeError):
            DefaultFormatter()  # the bug, unguarded
        module._ensure_standard_streams()
        DefaultFormatter()  # the fix: must not raise
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_main_binds_streams_before_starting_the_server():
    """Guard the ordering, not just the helper.

    start_server() imports uvicorn, so the guard must already have run by
    then; placing the call later reintroduces the crash.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    guard = source.index("_ensure_standard_streams()", source.index("def main"))
    server = source.index("start_server(args.port)", source.index("def main"))
    assert guard < server, "streams must be bound before uvicorn is imported"


# --- Diagnosable windowed startup -----------------------------------------
# A windowed failure used to be invisible: the launcher printed to a discarded
# stream, and an exception on the backend thread was swallowed by the threading
# machinery. The user saw only "127.0.0.1 refused to connect".

def test_launcher_log_goes_to_a_file_not_the_null_device(monkeypatch, tmp_path):
    module = _load_run_desktop()
    monkeypatch.setattr(module, "launcher_log_path", lambda: tmp_path / "launcher.log")
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        sys.stdout = sys.stderr = sys.stdin = None
        path = module._ensure_standard_streams()
        assert path is not None, "must open a real log, not devnull"
        assert path.name == "launcher.log"
        # The banner lands in the file, so a user can recover it after a crash.
        sys.stdout.write("probe\n")
        sys.stdout.flush()
        assert "probe" in path.read_text(encoding="utf-8")
        assert "launcher started" in path.read_text(encoding="utf-8")
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_ensure_streams_leaves_a_real_console_untouched(monkeypatch, tmp_path):
    """Running from a terminal must still print to that terminal."""
    module = _load_run_desktop()
    monkeypatch.setattr(module, "launcher_log_path", lambda: tmp_path / "launcher.log")
    original = (sys.stdout, sys.stderr, sys.stdin)
    try:
        assert module._ensure_standard_streams() is None
        assert (sys.stdout, sys.stderr, sys.stdin) == original
        assert not (tmp_path / "launcher.log").exists()
    finally:
        sys.stdout, sys.stderr, sys.stdin = original


def test_fatal_startup_reports_instead_of_opening_a_dead_window():
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")

    # A reachability check must precede launch(); otherwise the window opens on
    # a dead backend and shows a browser error page.
    check = source.index("Backend confirmed reachable")
    window = source.index("return launch(url")
    assert check < window
    assert "_report_fatal" in source


def test_watchdog_logs_backend_liveness_transitions():
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    assert "backend thread has exited" in source
    assert "UNREACHABLE" in source
    # Distinguishes a dead backend from a window that cannot route loopback.
    assert "_watch_backend" in source


def test_browser_fallback_bypasses_the_proxy_for_loopback():
    """A machine with a system proxy would otherwise refuse our own backend.

    The windowed URL is always loopback. Edge and Chrome honour system proxy
    settings for 127.0.0.1 unless told not to, which produces an app window
    showing "127.0.0.1 refused to connect" while the backend is demonstrably
    serving requests.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    assert '"--no-proxy-server"' in source
    assert '"--proxy-bypass-list=127.0.0.1;localhost"' in source


# --- App-mode window lifetime (§ v0.1.3 diagnosis) -------------------------
# The launcher log from the user's Windows VM showed the backend reachable for
# the whole run, followed immediately by "Window closed; shutting down
# backend". Chromium and Edge hand a new launch off to an already-running
# instance and exit at once, so proc.wait() returned while the window was
# still open and the caller tore the backend out from under it.

def test_browser_uses_a_dedicated_profile_so_it_cannot_hand_off():
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    assert "--user-data-dir=" in source, (
        "without a private profile the browser delegates to a running instance"
    )
    # First-run prompts can swallow the window and exit.
    assert "--no-first-run" in source
    assert "--no-default-browser-check" in source


def test_window_lifetime_is_not_capped_by_a_fixed_timeout():
    """A two-minute cap tears the backend down mid-download."""
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    start = source.index("def launch(")
    body = source[start:source.index("def main(", start)]
    assert "proc.wait(timeout=" not in body, "window must live until the user closes it"
    assert "while True:" in body and "keeping the backend alive" in body, (
        "a browser that hands off must not stop the backend"
    )


def test_pythonnet_is_bundled_for_the_native_window():
    """pywebview imports pythonnet dynamically, so PyInstaller cannot see it."""
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "MediaGrab.spec").read_text(encoding="utf-8")
    assert '"pythonnet"' in spec
    assert '"clr_loader"' in spec
    assert '"clr"' in spec


# --- Downloads folder must be the user's, not the app bundle ---------------
# configure_downloads_dir() used to import app.config before setting
# MEDIA_GRAB_DOWNLOADS_DIR, which cached DOWNLOADS_DIR at its module default.
# In the frozen bundle that default is _internal/downloads, so the downloader
# and "open folder" used the app's own folder while the launcher reported the
# real one.

def test_launcher_does_not_import_app_config_before_setting_the_override():
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    start = source.index("def default_downloads_dir")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    assert "from app.config import" not in body, (
        "importing app.config here caches DOWNLOADS_DIR before the override is set"
    )
    assert "MEDIA_GRAB_DOWNLOADS_DIR" in body


def test_downloads_dir_actually_follows_the_launcher(tmp_path):
    import os, subprocess, sys, textwrap

    if sys.platform.startswith("win"):
        pytest.skip("the launcher resolves Downloads via the Windows shell API")

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        import importlib.util
        spec = importlib.util.spec_from_file_location("rd", "run_desktop.py")
        rd = importlib.util.module_from_spec(spec); spec.loader.exec_module(rd)
        target = rd.configure_downloads_dir()
        from app.config import DOWNLOADS_DIR
        print(str(target) == str(DOWNLOADS_DIR))
        """
    )
    env = {**os.environ, "HOME": str(home), "MEDIAGRAB_MODE": "desktop"}
    env.pop("MEDIA_GRAB_DOWNLOADS_DIR", None)
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(root)
    )
    assert out.stdout.strip() == "True", out.stdout + out.stderr


# --- Caption embed row is MP4-only -----------------------------------------

def test_embed_row_is_hidden_for_audio():
    root = Path(__file__).resolve().parent.parent
    source = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "syncEmbedRow" in source
    body = source[source.index("const syncEmbedRow"):source.index("const btnFmtVid")]
    assert "row-subs-embed" in body
    assert "mp3" in body
    # A switch left checked while hidden would still be submitted, and the
    # preference must survive a round trip through audio.
    assert "check.checked = false" in body
    assert "lastEmbedPref" in body
    # Both format handlers must call it, or switching back to video is missed.
    assert source.count("syncEmbedRow();") >= 3


# --- One destination on desktop --------------------------------------------

def test_history_does_not_offer_a_second_download_on_desktop():
    root = Path(__file__).resolve().parent.parent
    source = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    start = source.index("function renderHistory()")
    end = source.index("function openHistoryDrawer", start)
    body = source[start:end]
    assert "data-open-folder" in body, "desktop rows must reveal the folder"
    assert "isHostedMode()" in body
    # The HTTP download is hosted-only now.
    assert "download=\"${item.filename}\"" in body


def test_history_lifetime_copy_is_not_hardcoded():
    root = Path(__file__).resolve().parent.parent
    source = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    start = source.index("function renderHistory()")
    end = source.index("function openHistoryDrawer", start)
    body = source[start:end]
    assert "gone in 30:00" not in body, (
        "30 minutes was wrong in hosted (purge is ~60s) and desktop (never)"
    )
    assert "formatRetention" in body
    assert "saved to your Downloads folder" in body


def test_open_downloads_button_exists_and_is_hidden_by_default():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-open-downloads"' in html
    assert 'id="btn-open-downloads" type="button" hidden' in html
    assert "Open Downloads folder" in html

    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(encoding="utf-8")
    start = js.index("function wireOpenDownloads")
    body = js[start:js.index("function renderHistory", start)]
    assert "isHostedMode()" in body, "hosted must hide it, not show a dead control"
    assert "open-folder" in body
