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


# --- Non-numeric format fields (§ user report: Facebook parse crash) --------

def _fmt(**kw):
    base = {'height': 1080, 'vcodec': 'avc1.640028', 'ext': 'mp4', 'acodec': 'none',
            'filesize': 10_000_000, 'tbr': 2000}
    base.update(kw)
    return base


def test_parse_metadata_survives_string_format_fields():
    """yt-dlp can emit numeric strings for height/filesize on some extractors.

    Comparing those against ints raised "'>' not supported between instances of
    'int' and 'str'", which surfaced to the user as an opaque processing
    failure rather than a usable diagnosis.
    """
    from app.services.extractor import parse_metadata

    info = {
        'title': 'Clip',
        'duration': 120,
        'formats': [
            _fmt(height='1080', filesize='90000000'),
            _fmt(height='720', filesize='40000000'),
            _fmt(height='480', filesize='12000000'),
        ],
    }
    meta = parse_metadata(info, "https://example.com/v")
    assert meta.title == "Clip"
    resolutions = [q.resolution for q in meta.qualities]
    assert "1080p" in resolutions and "720p" in resolutions
    # Values are coerced to real numbers, not left as strings.
    for q in meta.qualities:
        assert isinstance(q.approx_bytes, int)


def test_parse_metadata_prefers_the_larger_string_filesize():
    """The exact comparison that crashed: int filesize > stored string size."""
    from app.services.extractor import parse_metadata

    info = {
        'title': 'Clip',
        'duration': 120,
        'formats': [
            _fmt(height=1080, filesize='90000000'),
            _fmt(height=1080, filesize=95000000),
        ],
    }
    meta = parse_metadata(info, "https://example.com/v")
    top = next(q for q in meta.qualities if q.resolution == "1080p")
    assert top.approx_bytes == 95000000


def test_parse_metadata_skips_junk_numeric_fields_without_raising():
    from app.services.extractor import parse_metadata

    info = {
        'title': 'Clip',
        'duration': 120,
        'formats': [
            _fmt(height='not-a-number', filesize='also-bad'),
            _fmt(height=None, filesize=None),
            _fmt(height='1080', filesize='90000000'),
        ],
    }
    meta = parse_metadata(info, "https://example.com/v")
    assert meta.qualities, "must still offer at least one quality"


def test_parse_metadata_handles_string_tbr_without_estimate_crash():
    from app.services.extractor import parse_metadata

    info = {
        'title': 'Clip',
        'duration': 120,
        'formats': [
            _fmt(height='1080', filesize=None, tbr='2500.5', abr=None),
        ],
    }
    meta = parse_metadata(info, "https://example.com/v")
    top = next(q for q in meta.qualities if q.resolution == "1080p")
    assert top.approx_bytes > 0


def test_error_diagnosis_shows_no_placeholder_code():
    """A hardcoded code mislabelled every failure as MEDIA_INACCESSIBLE."""
    with open("static/choose.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "MEDIA_INACCESSIBLE" not in html
    assert 'id="error-diag-code" hidden' in html

    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    assert "function showError(msg, code)" in js
    # Code is opt-in and populated from the server, never a fixed string.
    assert "codeEl.textContent = value" in js
    assert "codeEl.hidden = !value" in js
