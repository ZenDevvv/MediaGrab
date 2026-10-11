import asyncio
from typing import Dict, Any, List
import yt_dlp
from app.api.schemas import InfoResponse, QualityOption, SubtitleOption
from app.services.ytdlp_opts import base_ytdl_opts

COMMON_RESOLUTIONS = [2160, 1440, 1080, 720, 480, 360]

def _sync_extract_info(url: str) -> Dict[str, Any]:
    ydl_opts = base_ytdl_opts(skip_download=True, extra={
        'extract_flat': False,
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

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

    # Build quality map for video
    res_map: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        height = f.get('height')
        if not height or height < 240:
            continue

        # Find closest standard resolution
        closest = min(COMMON_RESOLUTIONS, key=lambda r: abs(r - height))
        # Only accept if within 15% of standard resolution
        if abs(closest - height) > closest * 0.15:
            continue

        vcodec = (f.get('vcodec') or '').lower()
        ext = (f.get('ext') or '').lower()
        filesize = f.get('filesize') or f.get('filesize_approx')
        tbr = f.get('tbr') or (f.get('vbr', 0) + (f.get('abr', 0) or 128))

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

    # Fallback default qualities if none probed
    if not qualities:
        for r in [1080, 720, 480]:
            qualities.append(QualityOption(
                resolution=f"{r}p",
                approx_bytes=int(r * 50000),
                type="remux" if r <= 1080 else "encode"
            ))

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
        raise ValueError(f"Unable to read link: {msg.split('ERROR:')[-1].strip()}")
    except Exception as e:
        raise ValueError(f"Metadata extraction failed: {str(e)}")
