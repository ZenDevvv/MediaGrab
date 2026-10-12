import asyncio
import os
import re
import traceback
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp
from app import config
from app.api.schemas import DownloadRequest
from app.config import DOWNLOADS_DIR
from app.services.jobs import job_manager
from app.services.ytdlp_opts import base_ytdl_opts


class DownloadCancelled(Exception):
    """Raised inside the worker when a job is aborted mid-flight."""


def _raise_if_cancelled(job_id: str) -> None:
    job = job_manager.get_job(job_id)
    if job is not None and job.is_cancelled:
        raise DownloadCancelled("Download cancelled.")

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

def format_seconds(seconds: int) -> str:
    if seconds < 0:
        return "0:00"
    m = seconds // 60
    s = seconds % 60
    h = m // 60
    if h > 0:
        return f"{h}:{m % 60:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_size(bytes_val: Optional[int]) -> str:
    if not bytes_val or bytes_val <= 0:
        return "-- MB"
    mb = bytes_val / (1024 * 1024)
    if mb >= 10:
        return f"{round(mb)} MB"
    return f"{mb:.1f} MB"

def _handle_ffmpeg_progress(
    job_id: str,
    request: DownloadRequest,
    metrics: Dict[str, str]
):
    progress_status = metrics.get('progress')
    _raise_if_cancelled(job_id)
    out_time_us_str = metrics.get('out_time_us')
    speed_str = metrics.get('speed', '').strip()
    total_size_str = metrics.get('total_size')

    out_time_s = 0.0
    if out_time_us_str and out_time_us_str.isdigit():
        out_time_s = float(out_time_us_str) / 1_000_000.0
    elif 'out_time' in metrics:
        parts = metrics['out_time'].split(':')
        if len(parts) == 3:
            try:
                out_time_s = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except ValueError:
                pass

    speed_float = None
    if speed_str.endswith('x'):
        try:
            speed_float = float(speed_str[:-1])
        except ValueError:
            pass

    total_size = int(total_size_str) if total_size_str and total_size_str.isdigit() else None

    if request.trim and request.trim.end > request.trim.start:
        clip_duration = float(request.trim.end - request.trim.start)
        pct = (out_time_s / clip_duration * 98.0) if clip_duration > 0 else 0.0
        pct = min(98.0, max(0.5, pct))

        if speed_float and speed_float > 0.05:
            rem_s = max(0.0, clip_duration - out_time_s)
            eta_s = int(rem_s / speed_float)
            eta_disp = format_eta(eta_s)
            speed_disp = f"{speed_float:.1f}x speed"
        else:
            eta_disp = "--:--"
            speed_disp = "-- MB/s"

        got_disp = format_size(total_size) if total_size else "-- MB"

        curr_fmt = format_seconds(int(out_time_s))
        tot_fmt = format_seconds(int(clip_duration))
        stage_disp = f"Trimming segment ({curr_fmt} / {tot_fmt})"

        if progress_status == 'end':
            pct = 99.0
            eta_disp = "0:00"
            stage_disp = "Finalizing container..."

        job_manager.update_job(
            job_id,
            status="downloading" if pct < 99.0 else "processing",
            progress=round(pct, 1),
            speed=speed_disp,
            eta=eta_disp,
            got=got_disp,
            stage=stage_disp
        )
    else:
        stage_disp = "Remuxing & processing media..."
        if out_time_s > 0:
            stage_disp = f"Remuxing & processing ({format_seconds(int(out_time_s))})..."

        got_disp = format_size(total_size) if total_size else "-- MB"
        job_manager.update_job(
            job_id,
            status="processing",
            progress=99.0 if progress_status != 'end' else 99.5,
            got=got_disp,
            stage=stage_disp
        )

def create_ydl_opts(
    job_id: str,
    request: DownloadRequest,
    progress_port: Optional[int] = None,
    produced_files: Optional[list] = None,
) -> Dict[str, Any]:
    if produced_files is None:
        produced_files = []
    # Desktop writes straight into the user's Downloads folder, so the filename
    # is user-facing: bare title, de-duplicated on collision. Hosted keeps the
    # job-id prefix because its files are ephemeral and never seen twice
    # (§1.3 Decision #13).
    if config.USER_FACING_FILENAMES:
        out_template = str(DOWNLOADS_DIR / "%(title).150B.%(ext)s")
        # Direct yt-dlp's scratch files to a per-job staging directory. Without
        # this, partials land in the user's Downloads folder where they cannot
        # be told apart from a concurrent job's fragments -- mtime does not
        # discriminate, because both are written at the same moment (§1.3 #13).
        staging = config.DOWNLOADS_DIR / f".mediagrab-{job_id}"
        staging.mkdir(parents=True, exist_ok=True)
    else:
        out_template = str(DOWNLOADS_DIR / f"{job_id}_%(title).100B.%(ext)s")
        staging = None

    # Paths yt-dlp reports as finished, used for discovery and for purging
    # partials when a job is aborted.

    def progress_hook(d: Dict[str, Any]):
        _raise_if_cancelled(job_id)
        status = d.get('status')
        if status == 'finished':
            # yt-dlp resolves %(title)s and any collision suffix internally, so
            # its chosen filepath is the only reliable source of the output
            # name. Globbing by job id cannot work once the prefix is dropped
            # for user-facing filenames (§1.3 Decision #13).
            info = d.get('info_dict') or {}
            resolved = info.get('filepath') or info.get('_filename')
            if resolved:
                produced_files.append(Path(resolved))
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            speed = d.get('speed')
            eta = d.get('eta')

            info = d.get('info_dict', {})
            vcodec = info.get('vcodec')
            acodec = info.get('acodec')

            raw_pct = (downloaded / total * 100.0) if total > 0 else 0.0

            # Weight video stream 0..85%, audio stream 85..98% for multi-format downloads
            if vcodec and vcodec != 'none':
                pct = min(85.0, raw_pct * 0.85)
                stream_label = "video"
            elif acodec and acodec != 'none':
                pct = min(98.0, 85.0 + (raw_pct * 0.13))
                stream_label = "audio"
            else:
                pct = min(95.0, raw_pct * 0.95)
                stream_label = "stream"

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
                stage=f"Downloading {stream_label} payload"
            )
        elif status == 'finished':
            job_manager.update_job(
                job_id,
                status="processing",
                progress=98.5,
                stage="Remuxing & processing media"
            )

    def postprocessor_hook(d: Dict[str, Any]):
        status = d.get('status')
        postprocessor = d.get('postprocessor', '')
        if status == 'started':
            if "ExtractAudio" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=99.0, stage="Converting audio to MP3...")
            elif "EmbedThumbnail" in postprocessor or "Metadata" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=99.0, stage="Embedding tags & artwork...")
            elif "Merger" in postprocessor or "FFmpeg" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=99.0, stage="Remuxing video & audio...")
        elif status == 'finished':
            # The merge/remux has completed and the file now lives in the
            # output directory. This is the only reliable source for the final
            # location: the progress hook recorded the pre-merge paths inside
            # the staging directory, which yt-dlp has since moved.
            info = d.get('info_dict') or {}
            final = info.get('filepath') or info.get('_filename')
            if final and produced_files is not None:
                candidate = Path(final)
                if candidate not in produced_files:
                    produced_files.append(candidate)
            job_manager.update_job(job_id, status="processing", progress=99.5, stage="Finalizing container...")

    opts: Dict[str, Any] = {
        'outtmpl': out_template,
        **({'paths': {'home': str(config.DOWNLOADS_DIR), 'temp': str(staging)}} if staging else {}),
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [postprocessor_hook],
        'noplaylist': True,
        'concurrent_fragment_downloads': 4,
        'buffersize': 1024 * 1024,
        # Client, cookie and JS-runtime policy is shared with the extractor so
        # an inspect can never succeed where the download then fails.
        **base_ytdl_opts(),
    }

    # In-flight size backstop. yt-dlp evaluates this per stream, so it is a
    # coarse net rather than a precise cap -- the pre-flight estimate in
    # metadata.validate_request is what actually rejects oversized requests.
    if config.IS_HOSTED:
        # Must be a byte count. yt-dlp compares format sizes against this value
        # directly, so the human-readable "1024m" form raises
        # "'>' not supported between instances of 'int' and 'str'" and kills the
        # download. HOSTED_MAX_FILESIZE_ARG is documentation-only.
        opts['max_filesize'] = config.HOSTED_MAX_FILE_SIZE_BYTES

    if progress_port:
        opts['external_downloader_args'] = {
            'ffmpeg_o': ['-progress', f'tcp://127.0.0.1:{progress_port}']
        }
        opts['postprocessor_args'] = {
            'ffmpeg': ['-progress', f'tcp://127.0.0.1:{progress_port}']
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

        # Compatible MP4 video & M4A audio, fallback to best within resolution
        opts['format'] = (
            f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_h}]+bestaudio/"
            f"best[height<={max_h}][ext=mp4]/"
            f"best[height<={max_h}]/best"
        )
        opts['merge_output_format'] = 'mp4'

        postprocessors = []
        # Soft Subtitles Embedding
        if request.subtitles and request.subtitles.embed and request.subtitles.lang:
            opts['writesubtitles'] = True
            opts['writeautomaticsub'] = True
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
        opts['writeautomaticsub'] = True
        lang = request.subtitles.lang if request.subtitles and request.subtitles.lang else 'en'
        opts['subtitleslangs'] = [lang]
        opts['postprocessors'] = [
            {
                'key': 'FFmpegSubtitlesConvertor',
                'format': 'srt',
            }
        ]

    return opts

def _final_output_paths(result: Any) -> list:
    """Every real output path yt-dlp finished with.

    The progress hook records paths as each stream finishes, which are the
    pre-merge locations inside the per-job staging directory. Once a merge,
    remux or audio extraction runs, yt-dlp moves the result out of staging and
    those recorded paths no longer exist. Hosted mode papers over this with a
    job-id glob; desktop has no such fallback and raised "Output media file
    was not created" on every post-processed download.

    The return value of ``ydl.download()`` carries the authoritative final
    location, so use it.
    """
    paths = []
    if not isinstance(result, dict):
        return paths
    candidates = [result]
    candidates.extend(result.get("requested_downloads") or [])
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        for key in ("filepath", "_filename"):
            value = entry.get(key)
            if value:
                paths.append(Path(value))
                break
    return paths


def _sync_download(
    job_id: str,
    request: DownloadRequest,
    progress_port: Optional[int] = None,
    produced_files: Optional[list] = None,
):
    opts = create_ydl_opts(job_id, request, progress_port, produced_files)
    with yt_dlp.YoutubeDL(opts) as ydl:
        # extract_info, not download: download() returns an integer retcode,
        # not the info dict, so it cannot report where the file ended up.
        result = ydl.extract_info(request.url, download=True)

    if produced_files is not None:
        for path in _final_output_paths(result):
            if path not in produced_files:
                produced_files.append(path)

async def execute_download(job_id: str, request: DownloadRequest):
    initial_stage = "Connecting to media stream..."
    if request.trim and request.trim.end > request.trim.start:
        s_str = format_seconds(request.trim.start)
        e_str = format_seconds(request.trim.end)
        initial_stage = f"Trimming segment ({s_str} to {e_str})..."

    # Per-job scratch directory (desktop only). Recreated here rather than
    # captured in create_ydl_opts so purge_partials can reference it directly.
    staging_dir = (
        config.DOWNLOADS_DIR / f".mediagrab-{job_id}"
        if config.USER_FACING_FILENAMES else None
    )
    if staging_dir is not None:
        staging_dir.mkdir(parents=True, exist_ok=True)

    job_manager.update_job(job_id, status="extracting", stage=initial_stage)
    loop = asyncio.get_running_loop()

    server = None
    progress_port = None
    try:
        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                curr_metrics: Dict[str, str] = {}
                while line := await reader.readline():
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if '=' in line_str:
                        k, v = line_str.split('=', 1)
                        curr_metrics[k] = v
                        if k == 'progress':
                            _handle_ffmpeg_progress(job_id, request, curr_metrics)
                            curr_metrics = {}
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
        progress_port = server.sockets[0].getsockname()[1]
    except Exception:
        server = None
        progress_port = None

    # Paths yt-dlp reports as finished. Shared with the progress hook so abort
    # and completion both know the real output name (§1.3 Decision #13).
    produced_files: list = []

    def _cleanup_staging_dir() -> None:
        """Remove the now-empty per-job scratch directory, if present."""
        if staging_dir is None or not staging_dir.is_dir():
            return
        try:
            next(staging_dir.iterdir())
            return  # not empty: something is still in use, leave it alone
        except StopIteration:
            pass
        except OSError:
            return
        try:
            staging_dir.rmdir()
        except OSError:
            pass

    def purge_partials() -> None:
        """Remove every artifact this job produced, completed or not.

        On desktop the scratch directory is per-job, so this is exact: it can
        never touch a concurrent job's files. In hosted mode the job-id prefix
        provides the same isolation.
        """
        candidates = set(produced_files)
        if staging_dir is not None:
            if staging_dir.is_dir():
                for leftover in staging_dir.iterdir():
                    candidates.add(leftover)
        else:
            candidates.update(config.DOWNLOADS_DIR.glob(f"{job_id}_*"))

        for leftover in candidates:
            try:
                if leftover.is_file():
                    leftover.unlink()
            except OSError:
                pass
        if staging_dir is not None:
            try:
                if staging_dir.is_dir():
                    staging_dir.rmdir()
            except OSError:
                pass

    try:
        await loop.run_in_executor(
            None, _sync_download, job_id, request, progress_port, produced_files
        )
        _raise_if_cancelled(job_id)

        # Locate completed files. Prefer what yt-dlp reported; fall back to a glob
        # only in hosted mode, where the job-id prefix is guaranteed.
        matching_files = [p for p in produced_files if p.exists()]
        if not config.USER_FACING_FILENAMES:
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

        # Clean display filename (strip the internal job_id prefix, hosted only)
        display_name = primary_file.name
        if not config.USER_FACING_FILENAMES and display_name.startswith(f"{job_id}_"):
            display_name = display_name[len(job_id) + 1:]

        srt_display_name = None
        if srt_file:
            srt_display_name = srt_file.name
            if not config.USER_FACING_FILENAMES and srt_display_name.startswith(f"{job_id}_"):
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

    # The finished file has been moved out of the staging directory by
        # yt-dlp, so the directory is now empty and must go too, or every
        # desktop download would leave a .mediagrab-<id> folder behind.
        _cleanup_staging_dir()

    except DownloadCancelled:
        purge_partials()
        job_manager.update_job(job_id, status="cancelled", stage="Download cancelled")
    except yt_dlp.utils.DownloadError as e:
        raw = str(e)
        err_msg = raw.split('ERROR:')[-1].strip()
        job = job_manager.get_job(job_id)
        # yt-dlp may surface an abort as a generic DownloadError, so the flag
        # is the authoritative signal rather than the exception type.
        if job is not None and job.is_cancelled:
            purge_partials()
            job_manager.update_job(job_id, status="cancelled", stage="Download cancelled")
        elif "File is larger than max-filesize" in raw or "max-filesize" in raw:
            purge_partials()
            job_manager.update_job(
                job_id,
                status="error",
                error=(
                    "This file is larger than the hosted server limit. "
                    "Download the Desktop or Android app for unlimited file size."
                ),
            )
        else:
            job_manager.update_job(job_id, status="error", error=f"Download failed: {err_msg}")
    except Exception as e:
        # The desktop launcher redirects stdio into launcher.log, so writing
        # here is what lets a user hand over a log that actually explains the
        # failure. Previously an unexpected post-processing error surfaced only
        # as a one-line message in the UI with no traceback anywhere.
        traceback.print_exc()
        print(f"[MediaGrab] JOB {job_id} FAILED: {type(e).__name__}: {e}")
        job_manager.update_job(job_id, status="error", error=f"Processing failed: {str(e)}")
    finally:
        if server:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
