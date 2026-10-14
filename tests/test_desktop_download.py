import asyncio
import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_server():
    """Serves tests/fixtures over loopback, so real yt-dlp runs network-free."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


@pytest.fixture
def desktop_out(tmp_path, monkeypatch):
    out = tmp_path / "downloads"
    out.mkdir()
    monkeypatch.setenv("MEDIAGRAB_MODE", "desktop")
    monkeypatch.setenv("USER_FACING_FILENAMES", "true")
    monkeypatch.setenv("RETENTION_ENABLED", "false")
    monkeypatch.setattr("app.services.downloader.config.DOWNLOADS_DIR", out)
    monkeypatch.setattr("app.services.downloader.config.USER_FACING_FILENAMES", True)
    return out


def _grab(**fields):
    from app.api.schemas import DownloadRequest
    from app.services.downloader import execute_download
    from app.services.jobs import job_manager

    req = DownloadRequest(**fields)
    job = job_manager.create_job(req)
    asyncio.run(execute_download(job.id, req))
    return job_manager.get_job(job.id)


def _no_staging_left(out: Path) -> bool:
    return not any(p.name.startswith(".mediagrab-") for p in out.iterdir())


def test_desktop_download_from_a_local_server(fixture_server, desktop_out):
    """Network-free regression test for the desktop output-location bug."""
    j = _grab(url=f"{fixture_server}/sample.mp4", quality="720p", mode="mp4")
    assert j.status == "ready", f"{j.status}: {j.error}"
    # The output template must keep the separator before the extension.
    assert j.filename and j.filename.endswith(".mp4"), j.filename
    assert _no_staging_left(desktop_out), "per-job staging directory was left behind"


def test_second_grab_of_the_same_title_gets_its_own_file(fixture_server, desktop_out):
    """Regression: yt-dlp saw the earlier file in Downloads and returned it as
    "already downloaded" -- a full video came back as an earlier 10 s clip."""
    earlier = desktop_out / "sample.mp4"
    earlier.write_bytes(b"an earlier, different download")

    j = _grab(url=f"{fixture_server}/sample.mp4", quality="720p", mode="mp4")

    assert j.status == "ready", f"{j.status}: {j.error}"
    assert j.filename == "sample (1).mp4"
    assert earlier.read_bytes() == b"an earlier, different download"
    assert (desktop_out / "sample (1).mp4").read_bytes() == (FIXTURES / "sample.mp4").read_bytes()


def test_mp3_grab_does_not_delete_an_existing_mp4(fixture_server, desktop_out):
    """Regression: the MP3 job adopted the user's Title.mp4 as its source and
    deleted it after extracting the audio."""
    kept = desktop_out / "sample.mp4"
    kept.write_bytes(b"the user's video")

    j = _grab(url=f"{fixture_server}/sample.mp4", mode="mp3", bitrate=128)

    assert j.status == "ready", f"{j.status}: {j.error}"
    assert j.filename == "sample.mp3"
    assert kept.read_bytes() == b"the user's video"
    assert sorted(p.name for p in desktop_out.iterdir()) == ["sample.mp3", "sample.mp4"]


def test_subtitle_only_download(fixture_server, desktop_out):
    """Regression: SRT mode failed with "Output media file was not created" --
    yt-dlp reports subtitles outside the result's filepath, and the .srt was
    then purged as a partial."""
    j = _grab(url=f"{fixture_server}/captioned.html", mode="srt",
              subtitles={"lang": "en", "embed": False})

    assert j.status == "ready", f"{j.status}: {j.error}"
    assert j.filename.endswith(".srt"), j.filename
    assert j.srt_filename == j.filename
    srt = (desktop_out / j.filename).read_text(encoding="utf-8")
    assert "-->" in srt and "Hello from the fixture" in srt
    assert [p.name for p in desktop_out.iterdir()] == [j.filename], "only the .srt should be written"


def test_cancel_leaves_nothing_in_downloads(desktop_out, monkeypatch):
    """Regression: a cancelled MP3 grab left Title.avi.part and Title.jpg in
    the user's Downloads folder."""
    from app.services import downloader as dl
    from app.services.jobs import job_manager
    from app.api.schemas import DownloadRequest
    import yt_dlp

    kept = desktop_out / "Title.mp4"
    kept.write_bytes(b"keep me")

    class CancelledMidway:
        def __init__(self, opts):
            self.home = Path(opts["paths"]["home"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            (self.home / "Title.avi.part").write_bytes(b"x" * 1024)
            (self.home / "Title.jpg").write_bytes(b"thumb")
            job.request_cancel()
            raise yt_dlp.utils.DownloadError("ERROR: interrupted")

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", CancelledMidway)
    req = DownloadRequest(url="https://example.com/v", mode="mp3")
    job = job_manager.create_job(req)
    asyncio.run(dl.execute_download(job.id, req))

    assert job.status == "cancelled"
    assert [p.name for p in desktop_out.iterdir()] == ["Title.mp4"]
    assert kept.read_bytes() == b"keep me"


def test_free_name_never_overwrites(tmp_path):
    from app.services.downloader import _claim_free_name

    (tmp_path / "Clip.mp4").write_bytes(b"1")
    (tmp_path / "Clip (1).mp4").write_bytes(b"2")
    assert _claim_free_name(tmp_path, "Clip.mp4").name == "Clip (2).mp4"
    assert _claim_free_name(tmp_path, "Other.mp3").name == "Other.mp3"
    assert (tmp_path / "Clip.mp4").read_bytes() == b"1"


def test_hosted_subtitle_only_download_is_converted_to_srt(fixture_server, tmp_path, monkeypatch):
    """The SRT conversion never ran in either mode: hosted served the .vtt."""
    out = tmp_path / "hosted"
    out.mkdir()
    monkeypatch.setattr("app.services.downloader.config.DOWNLOADS_DIR", out)
    monkeypatch.setattr("app.services.downloader.DOWNLOADS_DIR", out)
    monkeypatch.setattr("app.services.downloader.config.USER_FACING_FILENAMES", False)

    j = _grab(url=f"{fixture_server}/captioned.html", mode="srt",
              subtitles={"lang": "en", "embed": False})

    assert j.status == "ready", f"{j.status}: {j.error}"
    assert j.filename.endswith(".srt") and not j.filename.startswith(j.id), j.filename
    assert "Hello from the fixture" in j.srt_path.read_text(encoding="utf-8")
