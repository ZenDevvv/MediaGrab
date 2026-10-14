"""Quality options and the download format rule (app/services/formats.py).

The format lists are trimmed copies of what yt-dlp reports for real sources.
"""
from app.services.formats import offered_qualities, video_format_spec, height_cap


def _v(fid, height, vcodec, ext, size):
    return {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
            "ext": ext, "filesize": size, "protocol": "https"}


def _a(fid, acodec, ext, size, abr):
    return {"format_id": fid, "vcodec": "none", "acodec": acodec, "ext": ext,
            "filesize": size, "abr": abr, "protocol": "https"}


def _pick(info, quality):
    import yt_dlp
    from app.services.formats import VIDEO_FORMAT_SORT, _clean
    probe = {"formats": _clean(info["formats"])}
    with yt_dlp.YoutubeDL({"quiet": True, "format_sort": VIDEO_FORMAT_SORT}) as ydl:
        ydl.sort_formats(probe)
        chosen = ydl._select_formats(probe["formats"], ydl.build_format_selector(video_format_spec(quality)))
    return [p["format_id"] for p in (chosen[0].get("requested_formats") or [chosen[0]])]


# "Me at the zoo": nothing above 240p, AV1 shipped inside .mp4.
ZOO = {"duration": 19, "formats": [
    _a("140", "mp4a.40.2", "m4a", 309288, 129), _a("251", "opus", "webm", 252182, 106),
    _v("160", 144, "avc1.4D400B", "mp4", 195278),
    _v("133", 240, "avc1.4D400C", "mp4", 433081),
    _v("395", 240, "av01.0.00M.08", "mp4", 223779),
    _v("242", 240, "vp9", "webm", 293080),
]}

# A 4K upload: H.264 stops at 1080p, above that only VP9.
BIG = {"duration": 635, "formats": [
    _a("140", "mp4a.40.2", "m4a", 10271496, 129), _a("251", "opus", "webm", 9000000, 140),
    _v("315", 2160, "vp9", "webm", 1362269481),
    _v("299", 1080, "avc1.64002A", "mp4", 257619653),
    _v("399", 1080, "av01.0.08M.08", "mp4", 150000000),
    _v("248", 1080, "vp9", "webm", 200000000),
    _v("298", 720, "avc1.4D4020", "mp4", 150524867),
]}

# archive.org: single files only, the bigger one an AVI.
ARCHIVE = {"duration": 596, "formats": [
    {"format_id": "0", "ext": "ogv", "height": 300, "filesize": 46935223},
    {"format_id": "1", "ext": "mp4", "height": 360, "filesize": 61878609},
    {"format_id": "2", "ext": "avi", "height": 720, "filesize": 332243668},
]}


def test_low_resolution_source_offers_only_what_exists():
    """Regression: a 240p video was offered a made-up 1080/720/480 at 51.5 MB."""
    q = offered_qualities(ZOO)
    assert [r for r, _, _ in q] == ["240p", "144p"]
    assert q[0][1] == 433081 + 309288, "priced as the H.264 video + AAC audio it downloads"


def test_h264_is_preferred_over_av1_in_mp4():
    """Regression: [ext=mp4] let YouTube's AV1-in-MP4 through; the README promises H.264."""
    assert _pick(ZOO, "240p") == ["133", "140"]
    assert _pick(BIG, "1080p") == ["299", "140"]


def test_4k_request_still_gets_4k():
    """Preferring H.264 must not quietly cap a 4K request at H.264's 1080p."""
    assert _pick(BIG, "2160p") == ["315", "140"]
    labels = [r for r, _, _ in offered_qualities(BIG)]
    assert labels == ["2160p", "1080p", "720p"]


def test_single_file_sources_prefer_mp4_and_price_it():
    """Regression: archive.org priced the 332 MB AVI; the download fetches the MP4."""
    assert _pick(ARCHIVE, "720p") == ["1"]
    assert offered_qualities(ARCHIVE) == [("360p", 61878609, "remux")]


def test_direct_file_without_heights_is_offered_as_original():
    info = {"url": "https://example.com/clip.mp4", "ext": "mp4", "format_id": "0",
            "vcodec": None, "acodec": None, "duration": 0}
    q = offered_qualities(info)
    assert [r for r, _, _ in q] == ["Original"]
    assert q[0][1] == 0, "unknown size stays unknown, not invented"


def test_quality_labels_map_to_height_caps():
    assert height_cap("720p") == 720
    assert height_cap(None) == 1080
    assert height_cap("Original") > 4320


# The Android chooser replays this rule over yt-dlp's JSON instead of calling
# the selector (android/.../engine/Formats.kt). MetadataMappingTest asserts the
# same list from the same file, so the two ports cannot drift apart.
MIXED_EXPECTED = [
    ("1080p", 120_000_000 + 9_536_000, "remux"),  # H.264 video-only + AAC, not the 150 MB muxed file
    ("360p", 8_000_000 + 9_536_000, "encode"),    # what a 720p request really gets, so 720p is not offered
    ("144p", 500_000 + 9_536_000, "remux"),
]


def test_shared_fixture_matches_the_android_port():
    import json
    from pathlib import Path
    info = json.loads((Path(__file__).parent / "fixtures" / "formats-mixed.json").read_text())
    assert offered_qualities(info) == MIXED_EXPECTED


def test_ytdlp_is_pinned_to_the_android_release():
    """Desktop/hosted and Android must run the same yt-dlp, or one breaks first."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    pins = re.findall(r"^yt-dlp==(\S+)$", (root / "requirements.txt").read_text(), re.M)
    lock = dict(
        line.split("=", 1) for line in (root / "android" / "ytdlp.lock").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert pins == [lock["version"]], f"requirements.txt {pins} vs android/ytdlp.lock {lock['version']}"
