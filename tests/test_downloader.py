import pytest
from app.api.schemas import DownloadRequest, TrimConfig, SubtitlesConfig
from app.services.downloader import (
    create_ydl_opts,
    format_speed,
    format_eta,
    format_size,
    sanitize_filename
)

def test_helpers():
    assert format_speed(4.2 * 1024 * 1024) == "4.2 MB/s"
    assert format_speed(500 * 1024) == "500 KB/s"
    assert format_speed(0) == "-- MB/s"

    assert format_eta(75) == "1:15"
    assert format_eta(0) == "0:00"

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
    assert "bestvideo[height<=1080]" in opts['format']
    assert opts['force_keyframes_at_cuts'] is True
    assert 'download_ranges' in opts
    assert opts['writesubtitles'] is True
    assert opts['subtitleslangs'] == ['en']

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
    assert opts['subtitleslangs'] == ['es']

    pp_keys = [pp['key'] for pp in opts['postprocessors']]
    assert 'FFmpegSubtitlesConvertor' in pp_keys
