from pathlib import Path

import pytest
from app.api.schemas import DownloadRequest, TrimConfig, SubtitlesConfig
from app.services.downloader import (
    create_ydl_opts,
    format_speed,
    format_eta,
    format_size,
    format_seconds,
    sanitize_filename
)

def test_helpers():
    assert format_speed(4.2 * 1024 * 1024) == "4.2 MB/s"
    assert format_speed(500 * 1024) == "500 KB/s"
    assert format_speed(0) == "-- MB/s"

    assert format_eta(75) == "1:15"
    assert format_eta(0) == "0:00"

    assert format_seconds(75) == "1:15"
    assert format_seconds(3665) == "1:01:05"
    assert format_seconds(0) == "0:00"

    assert format_size(29.1 * 1024 * 1024) == "29 MB"
    assert format_size(1.8 * 1024 * 1024) == "1.8 MB"

    assert sanitize_filename("How To / Make: Bread?") == "How-To-Make-Bread"

def test_create_ydl_opts_mp4():
    req = DownloadRequest(
        url="https://youtube.com/watch?v=sample",
        mode="mp4",
        quality="1080p",
        trim=TrimConfig(start=15, end=90),
        subtitles=SubtitlesConfig(lang="en", embed=True)
    )
    opts = create_ydl_opts("job12345", req)

    assert opts['merge_output_format'] == 'mp4'
    # The same rule /api/info prices qualities with (app/services/formats.py).
    from app.services.formats import VIDEO_FORMAT_SORT, video_format_spec
    assert opts['format'] == video_format_spec("1080p")
    assert "[height<=1080]" in opts['format']
    assert opts['format_sort'] == VIDEO_FORMAT_SORT
    assert opts['force_keyframes_at_cuts'] is True
    assert 'download_ranges' in opts
    assert opts['writesubtitles'] is True
    assert opts['writeautomaticsub'] is True
    assert opts['subtitleslangs'] == ['en']
    assert opts['concurrent_fragment_downloads'] == 4
    assert opts['buffersize'] == 1024 * 1024
    clients = opts['extractor_args']['youtube']['player_client']
    # PO-token-dependent clients must not be present.
    assert 'web' not in clients and 'mweb' not in clients

def test_create_ydl_opts_mp3():
    req = DownloadRequest(
        url="https://youtube.com/watch?v=sample",
        mode="mp3",
        bitrate=192
    )
    opts = create_ydl_opts("job12345", req)

    assert opts['format'] == 'bestaudio/best'
    assert opts['writethumbnail'] is True

    pp_keys = [pp['key'] for pp in opts['postprocessors']]
    assert 'FFmpegExtractAudio' in pp_keys
    assert 'FFmpegMetadata' in pp_keys
    assert 'EmbedThumbnail' in pp_keys

    audio_pp = next(pp for pp in opts['postprocessors'] if pp['key'] == 'FFmpegExtractAudio')
    assert audio_pp['preferredcodec'] == 'mp3'
    assert audio_pp['preferredquality'] == '192'

def test_create_ydl_opts_srt():
    req = DownloadRequest(
        url="https://youtube.com/watch?v=sample",
        mode="srt",
        subtitles=SubtitlesConfig(lang="es")
    )
    opts = create_ydl_opts("job12345", req)

    assert opts['skip_download'] is True
    assert opts['writesubtitles'] is True
    assert opts['writeautomaticsub'] is True
    assert opts['subtitleslangs'] == ['es']

    pp_keys = [pp['key'] for pp in opts['postprocessors']]
    assert 'FFmpegSubtitlesConvertor' in pp_keys

def test_create_ydl_opts_with_progress_port():
    req = DownloadRequest(
        url="https://youtube.com/watch?v=sample",
        mode="mp4",
        trim=TrimConfig(start=10, end=40)
    )
    opts = create_ydl_opts("job12345", req, progress_port=12345)

    assert 'external_downloader_args' in opts
    assert opts['external_downloader_args']['ffmpeg_o'] == ['-progress', 'tcp://127.0.0.1:12345']
    assert 'postprocessor_args' in opts
    assert opts['postprocessor_args']['ffmpeg'] == ['-progress', 'tcp://127.0.0.1:12345']

def test_handle_ffmpeg_progress_trim():
    from app.services.jobs import job_manager
    from app.services.downloader import _handle_ffmpeg_progress

    req = DownloadRequest(
        url="https://youtube.com/watch?v=sample",
        mode="mp4",
        trim=TrimConfig(start=358, end=495)  # 137s
    )
    job = job_manager.create_job(req)

    # 50% through (68.5s)
    metrics = {
        'out_time_us': '68500000',
        'speed': '2.0x',
        'total_size': '5242880',
        'progress': 'continue'
    }
    _handle_ffmpeg_progress(job.id, req, metrics)

    updated = job_manager.get_job(job.id)
    assert updated is not None
    assert updated.status == "downloading"
    assert updated.progress == 50.0  # 4 + (68.5 / 137) * (96 - 4)
    assert updated.speed == "2.0x speed"
    assert updated.eta == "0:34"  # (137 - 68.5) / 2.0 = 34.25s
    assert updated.got == "5.0 MB"
    assert "Trimming segment (1:08 / 2:17)" in updated.stage

    # Test completion packet
    _handle_ffmpeg_progress(job.id, req, {'progress': 'end'})
    final_updated = job_manager.get_job(job.id)
    assert final_updated.progress == 96.0
    assert final_updated.stage == "Finalizing container..."



# --- max_filesize typing (user report: every hosted download failed) --------

def test_hosted_max_filesize_is_a_byte_count_not_a_string(monkeypatch):
    """yt-dlp compares format sizes against max_filesize numerically.

    Passing the human-readable "1024m" made it evaluate `filesize > "1024m"`
    and raise "'>' not supported between instances of 'int' and 'str'", which
    surfaced as "Processing failed" on every hosted download.
    """
    from app.services.downloader import create_ydl_opts
    from app.api.schemas import DownloadRequest

    monkeypatch.setattr("app.services.downloader.config.IS_HOSTED", True)

    req = DownloadRequest(url="https://example.com/v", quality="1080p", mode="mp4")
    opts = create_ydl_opts("job1", req)

    assert "max_filesize" in opts
    assert isinstance(opts["max_filesize"], int)
    assert not isinstance(opts["max_filesize"], bool)
    assert opts["max_filesize"] > 0


def test_hosted_max_filesize_omitted_in_desktop_mode(monkeypatch):
    from app.services.downloader import create_ydl_opts
    from app.api.schemas import DownloadRequest

    monkeypatch.setattr("app.services.downloader.config.IS_HOSTED", False)

    req = DownloadRequest(url="https://example.com/v", quality="2160p", mode="mp4")
    opts = create_ydl_opts("job2", req)

    # The native app is unlimited by design and must not carry the cap.
    assert "max_filesize" not in opts


# --- Final output discovery after post-processing (§ v0.1.4 diagnosis) ----
# Desktop downloads failed with "Output media file was not created": yt-dlp
# moves the merged result out of the per-job staging directory, but the
# progress hook had recorded the pre-merge temp paths, which no longer exist.
# Hosted masked this with a job-id glob; desktop has no fallback.

def test_final_output_paths_reads_the_download_result():
    from app.services.downloader import _final_output_paths

    result = {
        "filepath": "/home/u/Downloads/clip.mp4",
        "requested_downloads": [
            {"filepath": "/home/u/Downloads/.mediagrab-1/clip.mp4"},
        ],
    }
    paths = _final_output_paths(result)
    assert paths[0] == Path("/home/u/Downloads/clip.mp4")
    assert len(paths) == 2


def test_final_output_paths_falls_back_to_filename_key():
    from app.services.downloader import _final_output_paths

    assert _final_output_paths({"_filename": "/x/a.mp4"}) == [Path("/x/a.mp4")]


def test_final_output_paths_tolerates_junk():
    from app.services.downloader import _final_output_paths

    assert _final_output_paths(None) == []
    assert _final_output_paths("nope") == []
    assert _final_output_paths({}) == []
    assert _final_output_paths({"requested_downloads": ["x", None]}) == []


def test_sync_download_records_the_post_processed_location(monkeypatch, tmp_path):
    """The temp path from the progress hook must not be the only candidate."""
    from app.services import downloader as dl
    from app.api.schemas import DownloadRequest

    final = tmp_path / "clip.mp4"
    final.write_bytes(b"data")
    stale = tmp_path / ".mediagrab-job1" / "clip.f137.mp4"

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            # Mirrors real behaviour: the hook saw a temp path, and the merged
            # result now lives outside the staging directory.
            return {"filepath": str(final)}

        def extract_info(self, url, download=True):
            return self.download([url])


    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)

    produced = []
    req = DownloadRequest(url="https://example.com/v", quality="720p", mode="mp4")
    dl._sync_download("job1", req, None, produced)

    existing = [p for p in produced if p.exists()]
    assert final in existing
    assert existing, "at least one recorded path must exist after the download"


def test_downloads_folder_uses_the_downloads_shell_folder_not_the_profile():
    """0x28 is CSIDL_PROFILE, which resolves C:\\Users\\<name>.

    That put every desktop download in the profile root instead of Downloads.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_desktop.py").read_text(encoding="utf-8")
    assert "SHGetFolderPathW(None, 0x05" in source, "must use CSIDL_DOWNLOADS"
    assert "SHGetFolderPathW(None, 0x28" not in source


def test_unexpected_job_errors_are_logged_with_a_traceback():
    root = Path(__file__).resolve().parent.parent
    source = (root / "app" / "services" / "downloader.py").read_text(encoding="utf-8")
    # The launcher redirects stdio to launcher.log, so these become the log the
    # user can paste.
    assert "traceback.print_exc()" in source
    assert "JOB {job_id} FAILED" in source


# --- one steady progress bar across video + audio ---------------------------

def _hooks_for(job_id):
    from app.api.schemas import DownloadRequest
    from app.services.downloader import create_ydl_opts

    opts = create_ydl_opts(job_id, DownloadRequest(url="https://example.com/v", mode="mp4"), None, [])
    return opts['progress_hooks'][0], opts['postprocessor_hooks'][0]


def test_progress_runs_forward_across_video_and_audio_streams():
    from app.services.jobs import job_manager

    req = DownloadRequest(url="https://example.com/v", mode="mp4")
    job = job_manager.create_job(req)
    progress, post = _hooks_for(job.id)

    video = {'format_id': 'v', 'filesize': 90_000_000, 'vcodec': 'avc1', 'acodec': 'none'}
    audio = {'format_id': 'a', 'filesize': 10_000_000, 'vcodec': 'none', 'acodec': 'mp4a'}
    info = {'requested_formats': [video, audio], **video}
    audio_info = {'requested_formats': [video, audio], **audio}

    seen = []

    def feed(hook, d):
        hook(d)
        seen.append(job_manager.get_job(job.id).progress)

    for done in (0, 30_000_000, 60_000_000, 90_000_000):
        feed(progress, {'status': 'downloading', 'downloaded_bytes': done, 'total_bytes': 90_000_000, 'info_dict': info})
    feed(progress, {'status': 'finished', 'downloaded_bytes': 90_000_000, 'total_bytes': 90_000_000, 'info_dict': info})
    # The first stream finishing must not announce processing or jump the bar.
    assert job_manager.get_job(job.id).status == "downloading"
    assert seen[-1] < 80

    for done in (0, 5_000_000, 10_000_000):
        feed(progress, {'status': 'downloading', 'downloaded_bytes': done, 'total_bytes': 10_000_000, 'info_dict': audio_info})
    feed(progress, {'status': 'finished', 'downloaded_bytes': 10_000_000, 'total_bytes': 10_000_000, 'info_dict': audio_info})
    assert job_manager.get_job(job.id).status == "processing"
    feed(post, {'status': 'started', 'postprocessor': 'Merger'})
    feed(post, {'status': 'finished', 'postprocessor': 'Merger', 'info_dict': {}})

    assert seen == sorted(seen), seen
    assert seen[0] >= 4.0
    assert max(b - a for a, b in zip(seen, seen[1:])) < 40  # no jump to the end
    assert seen[-1] == 96.0


def test_job_progress_never_moves_backward():
    from app.services.jobs import job_manager

    job = job_manager.create_job(DownloadRequest(url="https://example.com/v", mode="mp4"))
    job_manager.update_job(job.id, progress=70.0)
    job_manager.update_job(job.id, progress=40.0)
    assert job_manager.get_job(job.id).progress == 70.0
