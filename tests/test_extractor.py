import pytest
from app.services.extractor import parse_metadata

def test_parse_metadata_resolution_and_audio():
    sample_info = {
        "title": "Sample Sourdough Guide",
        "uploader": "Crumb & Co.",
        "duration": 252,
        "thumbnail": "https://example.com/thumb.jpg",
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "ext": "mp4",
                "filesize": 100000000
            },
            {
                "format_id": "248",
                "height": 1080,
                "vcodec": "vp9",
                "ext": "webm",
                "filesize": 80000000
            },
            {
                "format_id": "136",
                "height": 720,
                "vcodec": "avc1.4d401f",
                "ext": "mp4",
                "filesize": 50000000
            },
            {
                "format_id": "251",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 129
            }
        ],
        "subtitles": {
            "en": [{"name": "English"}]
        },
        "automatic_captions": {
            "es": [{"name": "Spanish (auto)"}]
        }
    }

    result = parse_metadata(sample_info, "https://youtube.com/watch?v=sample")

    assert result.title == "Sample Sourdough Guide"
    assert result.uploader == "Crumb & Co."
    assert result.duration == 252
    assert result.source_audio_kbps == 129
    assert result.source_audio_codec == "Opus"

    # Qualities
    res_names = [q.resolution for q in result.qualities]
    assert "1080p" in res_names
    assert "720p" in res_names

    q1080 = next(q for q in result.qualities if q.resolution == "1080p")
    assert q1080.type == "remux" # AVC1 codec was present

    # Subtitles
    langs = [s.lang for s in result.subtitles]
    assert "en" in langs
    assert "es" in langs
    sub_en = next(s for s in result.subtitles if s.lang == "en")
    assert sub_en.is_auto is False
    sub_es = next(s for s in result.subtitles if s.lang == "es")
    assert sub_es.is_auto is True


def test_sync_extract_info_opts(monkeypatch):
    from unittest.mock import MagicMock
    import yt_dlp
    from app.services.extractor import _sync_extract_info

    captured_opts = {}
    class MockYoutubeDL:
        def __init__(self, opts):
            nonlocal captured_opts
            captured_opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            return {"title": "Mock Video", "formats": []}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", MockYoutubeDL)
    res = _sync_extract_info("https://youtube.com/watch?v=mock")

    assert res["title"] == "Mock Video"
    assert captured_opts["skip_download"] is True
    # "web"/"mweb" require a PO token for GVS and fail without one, so they
    # must not be in the client list. See config.YTDLP_PLAYER_CLIENTS.
    clients = captured_opts["extractor_args"]["youtube"]["player_client"]
    assert "web" not in clients and "mweb" not in clients
    assert "default" in clients
