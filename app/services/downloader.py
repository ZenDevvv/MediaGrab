import asyncio
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp
from app.api.schemas import DownloadRequest
from app.config import DOWNLOADS_DIR
from app.services.jobs import job_manager

def sanitize_filename(name: str) -> str:
    # Remove filesystem illegal characters
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = re.sub(r'[\s_]+', '-', cleaned).strip("-")
    return cleaned[:100] or "media"

def format_speed(bytes_per_sec: Optional[float]) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "-- MB/s"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    return f"{bytes_per_sec / 1024:.0f} KB/s"

def format_eta(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    m = seconds // 60
    s = seconds % 60
    return f"{m}:{s:02d}"

def format_size(bytes_val: Optional[int]) -> str:
    if not bytes_val or bytes_val <= 0:
        return "-- MB"
    mb = bytes_val / (1024 * 1024)
    if mb >= 10:
        return f"{round(mb)} MB"
    return f"{mb:.1f} MB"

def create_ydl_opts(job_id: str, request: DownloadRequest) -> Dict[str, Any]:
    out_template = str(DOWNLOADS_DIR / f"{job_id}_%(title).100B.%(ext)s")

    def progress_hook(d: Dict[str, Any]):
        status = d.get('status')
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            speed = d.get('speed')
            eta = d.get('eta')

            pct = (downloaded / total * 100.0) if total > 0 else 0.0
            pct = min(99.0, max(0.0, pct)) # Reserve 100% for post-processing finish

            speed_str = format_speed(speed)
            eta_str = format_eta(eta)
            got_str = f"{format_size(downloaded)} / {format_size(total)}" if total else format_size(downloaded)

            job_manager.update_job(
                job_id,
                status="downloading",
                progress=round(pct, 1),
                speed=speed_str,
                eta=eta_str,
                got=got_str,
                stage="Downloading stream payload"
            )
        elif status == 'finished':
            job_manager.update_job(
                job_id,
                status="processing",
                progress=99.0,
                stage="Remuxing & processing media"
            )

    opts: Dict[str, Any] = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'noplaylist': True,
    }

    # Time Trimming
    if request.trim and request.trim.end > request.trim.start:
        opts['download_ranges'] = yt_dlp.utils.download_range_func(
            None, [(request.trim.start, request.trim.end)]
        )
        opts['force_keyframes_at_cuts'] = True

    # Mode 1: Video (MP4)
    if request.mode == "mp4":
        height_map = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
        max_h = height_map.get(request.quality or "1080p", 1080)

        # Prioritize AVC1 / MP4 direct remux, fallback to best within resolution
        opts['format'] = (
            f"bestvideo[height<={max_h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_h}]+bestaudio/"
            f"best[height<={max_h}]/best"
        )
        opts['merge_output_format'] = 'mp4'

        postprocessors = []
        # Soft Subtitles Embedding
        if request.subtitles and request.subtitles.embed and request.subtitles.lang:
            opts['writesubtitles'] = True
            opts['subtitleslangs'] = [request.subtitles.lang]
            postprocessors.append({'key': 'FFmpegEmbedSubtitle'})

        if postprocessors:
            opts['postprocessors'] = postprocessors

    # Mode 2: Audio (MP3)
    elif request.mode == "mp3":
        opts['format'] = 'bestaudio/best'
        bitrate = str(request.bitrate or 192)
        opts['writethumbnail'] = True
        opts['postprocessors'] = [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': bitrate,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            }
        ]

    # Mode 3: Subtitles / Transcript (SRT)
    elif request.mode == "srt":
        opts['skip_download'] = True
        opts['writesubtitles'] = True
        lang = request.subtitles.lang if request.subtitles and request.subtitles.lang else 'en'
        opts['subtitleslangs'] = [lang]
        opts['postprocessors'] = [
            {
                'key': 'FFmpegSubtitlesConvertor',
                'format': 'srt',
            }
        ]

    return opts

def _sync_download(job_id: str, request: DownloadRequest):
    opts = create_ydl_opts(job_id, request)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([request.url])

async def execute_download(job_id: str, request: DownloadRequest):
    job_manager.update_job(job_id, status="extracting", stage="Initializing stream connection")
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, _sync_download, job_id, request)

        # Locate completed files
        matching_files = list(DOWNLOADS_DIR.glob(f"{job_id}_*"))
        if not matching_files:
            raise FileNotFoundError("Output media file was not created.")

        # Identify primary media file and optional subtitle file
        primary_file: Optional[Path] = None
        srt_file: Optional[Path] = None

        for f in matching_files:
            if f.suffix.lower() == ".srt":
                srt_file = f
            elif f.suffix.lower() in [".mp4", ".mp3", ".m4a", ".webm"]:
                primary_file = f

        if not primary_file and srt_file:
            primary_file = srt_file

        if not primary_file:
            primary_file = matching_files[0]

        file_stat = primary_file.stat()
        size_str = format_size(file_stat.st_size)

        # Clean display filename (strip internal job_id prefix)
        display_name = primary_file.name
        if display_name.startswith(f"{job_id}_"):
            display_name = display_name[len(job_id) + 1:]

        srt_display_name = None
        if srt_file:
            srt_display_name = srt_file.name
            if srt_display_name.startswith(f"{job_id}_"):
                srt_display_name = srt_display_name[len(job_id) + 1:]

        job_manager.update_job(
            job_id,
            status="ready",
            progress=100.0,
            file_path=primary_file,
            filename=display_name,
            size=size_str,
            srt_path=srt_file,
            srt_filename=srt_display_name,
            stage="Ready to save"
        )

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e).split('ERROR:')[-1].strip()
        job_manager.update_job(job_id, status="error", error=f"Download failed: {err_msg}")
    except Exception as e:
        job_manager.update_job(job_id, status="error", error=f"Processing failed: {str(e)}")
