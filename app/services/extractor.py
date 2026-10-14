import asyncio
from typing import Dict, Any, List
import yt_dlp
from app.api.schemas import InfoResponse, QualityOption, SubtitleOption
from app.services.formats import offered_qualities
from app.services.youtube_circuit import is_youtube_url
from app.services.ytdlp_opts import base_ytdl_opts

COMMON_RESOLUTIONS = [2160, 1440, 1080, 720, 480, 360]


class UpstreamBotCheck(ValueError):
    """The upstream site refused this server's address (YouTube bot check).

    Distinct from a bad URL: retrying, trimming or changing quality cannot
    help. The only fixes are a different egress address or the client running
    the request from the user's own network, so callers should surface this as
    an upgrade path rather than an error.
    """

    #: Machine-readable marker used by the frontend to pick the upsell copy.
    code = "UPSTREAM_BLOCKED"

#: Substrings that mean "this server's address is refused", as opposed to
#: "this particular link is bad". YouTube answers a refused datacenter address
#: in several different ways depending on which layer rejects it, and matching
#: only one of them was the reason the circuit breaker kept re-opening the gate:
#: every unrecognised refusal produced a generic error, left the breaker
#: closed, and invited another request, which is how a retry loop turns into
#: the HTTP 429 the user then sees.
_UPSTREAM_REFUSALS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
    "http error 403",
    "rate-limit",
    "rate limit",
)


def is_upstream_refusal(message: str) -> bool:
    """True when ``message`` is an address-level refusal rather than a bad link.

    Only meaningful for sites that refuse by address. A 403 on an arbitrary
    host may just as easily mean the link is gone, so callers must gate this on
    the platform rather than apply it universally.
    """
    if not message:
        return False
    lowered = message.lower()
    return any(signal in lowered for signal in _UPSTREAM_REFUSALS)

def _sync_extract_info(url: str) -> Dict[str, Any]:
    ydl_opts = base_ytdl_opts(skip_download=True, extra={
        'extract_flat': False,
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def _to_number(value: Any, default: float = 0.0) -> float:
    """Coerce a yt-dlp format field to a number.

    yt-dlp's format dicts are not uniformly typed: most extractors emit ints and
    floats, but several (Facebook and Instagram among them) can yield numeric
    strings for ``height``/``filesize``. Comparing those against ints raises
    ``'>' not supported between instances of 'int' and 'str'`` deep inside
    parsing, which surfaces to the user as an opaque processing failure rather
    than a diagnosis they can act on.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _estimated_qualities(formats: List[Dict[str, Any]], duration: int) -> List[QualityOption]:
    """Per-resolution estimate from the raw format list, without yt-dlp's selector.

    Only the fallback for when selection fails (formats.offered_qualities is
    the real answer): it buckets by nearest standard height and prices the
    largest format in each, which can overstate what will download.
    """
    res_map: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        height = int(_to_number(f.get('height'), 0))
        if not height or height < 240:
            continue

        # Find closest standard resolution
        closest = min(COMMON_RESOLUTIONS, key=lambda r: abs(r - height))
        # Only accept if within 15% of standard resolution
        if abs(closest - height) > closest * 0.15:
            continue

        vcodec = (f.get('vcodec') or '').lower()
        ext = (f.get('ext') or '').lower()
        filesize = int(_to_number(f.get('filesize') or f.get('filesize_approx'), 0))
        tbr = _to_number(f.get('tbr'), 0) or (_to_number(f.get('vbr'), 0) + (_to_number(f.get('abr'), 0) or 128))

        if not filesize and tbr and duration:
            filesize = int(tbr * 1000 / 8 * duration)

        is_remux = ('avc1' in vcodec or 'h264' in vcodec or ext == 'mp4') and closest <= 1080

        if closest not in res_map or (filesize and filesize > res_map[closest]['size']):
            res_map[closest] = {
                'size': filesize or 0,
                'type': 'remux' if is_remux else 'encode'
            }

    qualities: List[QualityOption] = []
    for r in sorted(res_map.keys(), reverse=True):
        qualities.append(QualityOption(
            resolution=f"{r}p",
            approx_bytes=res_map[r]['size'],
            type=res_map[r]['type']
        ))

    return qualities


def parse_metadata(info: Dict[str, Any], url: str) -> InfoResponse:
    title = info.get('title') or 'Untitled'
    uploader = info.get('uploader') or info.get('channel') or 'Unknown'
    duration = int(info.get('duration') or 0)
    thumbnail = info.get('thumbnail')

    formats = info.get('formats') or []

    # Detect audio stream characteristics
    audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    source_audio_kbps = 128
    source_audio_codec = "Opus"
    if audio_formats:
        # Sort by audio bitrate or quality
        best_audio = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
        source_audio_kbps = int(best_audio.get('abr') or best_audio.get('tbr') or 128)
        acodec = best_audio.get('acodec', '')
        if 'opus' in acodec.lower():
            source_audio_codec = 'Opus'
        elif 'mp4a' in acodec.lower() or 'aac' in acodec.lower():
            source_audio_codec = 'AAC'
        elif acodec:
            source_audio_codec = acodec

    qualities: List[QualityOption] = []
    try:
        qualities = [
            QualityOption(resolution=r, approx_bytes=b, type=t)
            for r, b, t in offered_qualities(info)
        ]
    except Exception as e:
        # Selection runs yt-dlp internals over extractor output; a surprise
        # there must cost the precise list, not the whole inspect.
        print(f"[MediaGrab] quality selection failed, using estimates: {type(e).__name__}: {e}")
        qualities = _estimated_qualities(formats, duration)

    # Subtitles extraction
    subtitles: List[SubtitleOption] = []
    # Creator subtitles
    raw_subs = info.get('subtitles') or {}
    for lang, sub_list in raw_subs.items():
        if sub_list:
            name = sub_list[0].get('name') or lang.upper()
            subtitles.append(SubtitleOption(lang=lang, label=name, is_auto=False))

    # Automatic captions (limit to top common if extensive)
    auto_subs = info.get('automatic_captions') or {}
    for lang, sub_list in auto_subs.items():
        if sub_list and not any(s.lang == lang for s in subtitles):
            name = sub_list[0].get('name') or f"{lang.upper()} (auto)"
            subtitles.append(SubtitleOption(lang=lang, label=name, is_auto=True))

    return InfoResponse(
        url=url,
        title=title,
        uploader=uploader,
        duration=duration,
        thumbnail=thumbnail,
        source_audio_kbps=source_audio_kbps,
        source_audio_codec=source_audio_codec,
        qualities=qualities,
        subtitles=subtitles[:12] # Limit list to reasonable size for UI
    )

async def inspect_url(url: str) -> InfoResponse:
    loop = asyncio.get_running_loop()
    try:
        raw_info = await loop.run_in_executor(None, _sync_extract_info, url)
        return parse_metadata(raw_info, url)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Unsupported URL" in msg:
            raise ValueError("That URL is not supported.")
        elif "Private video" in msg:
            raise ValueError("This video is private.")
        elif "Video unavailable" in msg:
            raise ValueError("This video is unavailable.")
        elif is_youtube_url(url) and is_upstream_refusal(msg):
            # YouTube refuses anonymous requests from this server's address,
            # and says so in whichever way suits the layer that rejected it:
            # a bot check during extraction, a 403 on the media request, or a
            # 429 once it has decided this address is a problem. All of them
            # mean the same thing and none of them improve on retry.
            #
            # Measured on the hosted VM: identical across all 11 player
            # clients, unchanged by a JS runtime, worse with TLS
            # impersonation, and unchanged on yt-dlp nightly. It is address
            # reputation, so the honest remedy is to run the request from the
            # user's own connection -- which is exactly what the native apps do.
            raise UpstreamBotCheck(
                "YouTube is blocking requests from this server's address, so this "
                "link can't be read here. The Desktop and Android apps download from "
                "your own connection, where this restriction doesn't apply."
            )
        raise ValueError(f"Unable to read link: {msg.split('ERROR:')[-1].strip()}")
    except Exception as e:
        raise ValueError(f"Metadata extraction failed: {str(e)}")
