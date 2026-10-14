"""Which format a video download fetches, shared by the downloader and /api/info.

One rule, used twice: the downloader passes it to yt-dlp, and the info endpoint
runs yt-dlp's own selector with it over the probed formats to decide which
qualities to offer and what each will weigh. Before this the two disagreed --
the chooser offered a made-up 1080/720/480 for a 240p video and priced a
332 MB AVI the downloader would never pick.

The rule: the highest resolution within the cap, and at that resolution H.264
video with AAC audio, so the MP4 plays everywhere without re-encoding. YouTube
also ships AV1 inside .mp4, which the previous ``[ext=mp4]`` preference let
through. Resolution is ranked first so a 4K request still gets 4K (VP9/AV1)
rather than quietly dropping to the best H.264, which tops out at 1080p.

Separate video+audio streams merge into MP4 whatever their container. A
single-file format has no such step, so among those MP4 is preferred: an
"MP4" download of an archive.org item must not come back as its AVI.

android/.../YoutubeDlEngine.kt mirrors this; keep them in step.
"""

from typing import Any, Dict, List, Optional, Tuple

STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]

# Resolution first, then codec preference within it.
VIDEO_FORMAT_SORT = ["res", "vcodec:h264", "acodec:aac"]

# A label with no number ("Original", for sources that report no heights) is
# uncapped; the trailing "/b" picks the best there is.
UNCAPPED = 100000


def height_cap(quality: Optional[str]) -> int:
    digits = "".join(ch for ch in (quality or "") if ch.isdigit())
    if not quality:
        return 1080
    return int(digits) if digits else UNCAPPED


def video_format_spec(quality: Optional[str]) -> str:
    h = height_cap(quality)
    return f"bv[height<={h}]+ba/b[height<={h}][ext=mp4]/b[height<={h}]/b"


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _clean(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies yt-dlp's selector can compare without tripping.

    Some extractors (Facebook, Instagram) emit numeric strings for height and
    size; the selector's ``height<=`` filter and sort would raise on those.
    """
    cleaned = []
    for i, f in enumerate(formats):
        if not isinstance(f, dict):
            continue
        c = dict(f)
        for key in ("height", "width", "filesize", "filesize_approx", "tbr", "vbr", "abr"):
            n = _number(c.get(key))
            if n > 0:
                c[key] = int(n) if key in ("height", "width", "filesize", "filesize_approx") else n
            else:
                c.pop(key, None)
        c.setdefault("format_id", str(i))
        c.setdefault("ext", "unknown_video")
        # Sorting derives the protocol from the URL and raises without either.
        if not c.get("url"):
            c.setdefault("protocol", "https")
        cleaned.append(c)
    return cleaned


def _parts(chosen: Dict[str, Any]) -> List[Dict[str, Any]]:
    return chosen.get("requested_formats") or [chosen]


def _height(chosen: Dict[str, Any]) -> int:
    return max((int(p.get("height") or 0) for p in _parts(chosen)), default=0)


def _size(chosen: Dict[str, Any], duration: float) -> int:
    total = 0
    for p in _parts(chosen):
        size = p.get("filesize") or p.get("filesize_approx")
        if not size and duration:
            tbr = p.get("tbr") or ((p.get("vbr") or 0) + (p.get("abr") or 0))
            size = tbr * 1000 / 8 * duration
        total += int(size or 0)
    return total


def _kind(chosen: Dict[str, Any]) -> str:
    video = [p for p in _parts(chosen) if (p.get("vcodec") or "") != "none"]
    codecs = " ".join((p.get("vcodec") or "").lower() for p in video)
    if "avc1" in codecs or "h264" in codecs:
        return "remux"
    if not codecs.strip() and all((p.get("ext") or "") == "mp4" for p in video):
        return "remux"
    return "encode"


def ceiling_label(height: int) -> Optional[str]:
    for h in sorted(STANDARD_HEIGHTS):
        if height <= h:
            return f"{h}p"
    return None


def offered_qualities(info: Dict[str, Any]) -> List[Tuple[str, int, str]]:
    """``(resolution, approx_bytes, type)`` for each quality worth offering.

    A resolution is offered only when the downloader, asked for it, would get
    a video whose height rounds up to exactly that label -- so the list holds
    what the source really has and each size is the file that will download.
    """
    import yt_dlp

    raw = info.get("formats")
    if not raw and info.get("url"):
        # A direct file link: the info dict is itself the only format.
        raw = [{k: v for k, v in info.items() if k != "formats"}]
    formats = _clean(raw or [])
    if not formats:
        return []
    duration = _number(info.get("duration"))
    probe = {"formats": formats}
    if info.get("_format_sort_fields"):
        probe["_format_sort_fields"] = info["_format_sort_fields"]

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                           "format_sort": VIDEO_FORMAT_SORT}) as ydl:
        ydl.sort_formats(probe)

        def pick(quality: str) -> Optional[Dict[str, Any]]:
            selector = ydl.build_format_selector(video_format_spec(quality))
            chosen = ydl._select_formats(probe["formats"], selector)
            return chosen[0] if chosen else None

        options = []
        if not any(f.get("height") for f in formats):
            chosen = pick("Original")
            if chosen:
                options.append(("Original", _size(chosen, duration), _kind(chosen)))
            return options

        for h in STANDARD_HEIGHTS:
            label = f"{h}p"
            chosen = pick(label)
            if chosen and ceiling_label(_height(chosen)) == label:
                options.append((label, _size(chosen, duration), _kind(chosen)))
        return options
