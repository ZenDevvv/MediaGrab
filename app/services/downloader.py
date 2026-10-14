import asyncio
import os
import re
import shutil
import traceback
from pathlib import Path
from typing import Dict, Any, Optional
import yt_dlp
from app import config
from app.api.schemas import DownloadRequest
from app.config import DOWNLOADS_DIR
from app.services.formats import VIDEO_FORMAT_SORT, video_format_spec
from app.services.jobs import job_manager
from app.services.progress import (
    DOWNLOAD_END, PROCESS_END, PROCESS_START, READ_END, SAVE_END, TAGS_AT,
    VIDEO_SHARE, download_pct,
)
from app.services.youtube_circuit import circuit as youtube_circuit, is_youtube_url
from app.services.extractor import is_upstream_refusal
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
        fraction = (out_time_s / clip_duration) if clip_duration > 0 else 0.0
        pct = min(PROCESS_END, max(READ_END, READ_END + fraction * (PROCESS_END - READ_END)))

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
            pct = PROCESS_END
            eta_disp = "0:00"
            stage_disp = "Finalizing container..."

        job_manager.update_job(
            job_id,
            status="downloading" if pct < PROCESS_END else "processing",
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
            # No measure of how long a remux takes: report the stage and let
            # the page ease through the processing slice.
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
        out_template = "%(title).150B.%(ext)s"
        # yt-dlp writes everything -- partials, intermediates, thumbnails and
        # the finished file -- into a per-job staging directory, and
        # execute_download moves the result into Downloads under a free name.
        #
        # Writing straight into Downloads meant yt-dlp saw the user's earlier
        # files: a second grab of the same title returned the old file as
        # "already downloaded" (a full video came back as an earlier 10 s
        # clip), and an MP3 grab adopted an existing Title.mp4 as its source
        # and then deleted it after extracting the audio. Partials there also
        # could not be told apart from a concurrent job's (§1.3 #13).
        staging = config.DOWNLOADS_DIR / f".mediagrab-{job_id}"
        staging.mkdir(parents=True, exist_ok=True)
    else:
        out_template = str(DOWNLOADS_DIR / f"{job_id}_%(title).100B.%(ext)s")
        staging = None

    # Paths yt-dlp reports as finished, used for discovery and for purging
    # partials when a job is aborted.

    # Streams already fetched (video, then audio), so one bar can span them.
    finished_bytes = 0
    finished_streams = 0

    def _requested(info: Dict[str, Any]):
        return info.get('requested_formats') or [info]

    def progress_hook(d: Dict[str, Any]):
        nonlocal finished_bytes, finished_streams
        _raise_if_cancelled(job_id)
        status = d.get('status')
        info = d.get('info_dict') or {}
        if status == 'finished':
            # yt-dlp resolves %(title)s internally, so its chosen filepath is
            # the only reliable source of the output name. Globbing by job id
            # cannot work once the prefix is dropped for user-facing filenames
            # (§1.3 Decision #13).
            resolved = info.get('filepath') or info.get('_filename')
            if resolved:
                produced_files.append(Path(resolved))
            finished_streams += 1
            finished_bytes += int(d.get('total_bytes') or d.get('downloaded_bytes') or 0)
            # Only the last stream means the download is over. The first of a
            # video+audio pair used to announce "remuxing" here, which jumped
            # the bar to 98% and then dropped it when the audio began.
            if finished_streams >= len(_requested(info)):
                job_manager.update_job(
                    job_id,
                    status="processing",
                    progress=DOWNLOAD_END,
                    stage="Processing media",
                )
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0) or 0
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            speed = d.get('speed')
            eta = d.get('eta')

            vcodec = info.get('vcodec')
            acodec = info.get('acodec')
            if vcodec and vcodec != 'none':
                stream_label = "video"
            elif acodec and acodec != 'none':
                stream_label = "audio"
            else:
                stream_label = "stream"

            # One fraction across every stream. With the sizes yt-dlp already
            # knows, it is the bytes fetched over the bytes to fetch; without
            # them, video is weighted over audio.
            parts = _requested(info)
            sizes = [int(p.get('filesize') or p.get('filesize_approx') or 0) for p in parts]
            if len(parts) > 1 and all(sizes):
                fraction = (finished_bytes + downloaded) / float(sum(sizes))
            elif len(parts) > 1:
                raw = (downloaded / total) if total > 0 else 0.0
                fraction = (raw * VIDEO_SHARE if finished_streams == 0
                            else VIDEO_SHARE + raw * (1.0 - VIDEO_SHARE))
            else:
                fraction = (downloaded / total) if total > 0 else 0.0
            pct = download_pct(fraction)

            speed_str = format_speed(speed)
            eta_str = format_eta(eta)
            got_str = f"{format_size(downloaded)} / {format_size(total)}" if total else format_size(downloaded)

            job_manager.update_job(
                job_id,
                status="downloading",
                progress=pct,
                speed=speed_str,
                eta=eta_str,
                got=got_str,
                stage=f"Downloading {stream_label} payload"
            )

    def postprocessor_hook(d: Dict[str, Any]):
        status = d.get('status')
        postprocessor = d.get('postprocessor', '')
        if status == 'started':
            if "ExtractAudio" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=PROCESS_START, stage="Converting audio to MP3...")
            elif "EmbedThumbnail" in postprocessor or "Metadata" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=TAGS_AT, stage="Embedding tags & artwork...")
            elif "Merger" in postprocessor or "FFmpeg" in postprocessor:
                job_manager.update_job(job_id, status="processing", progress=PROCESS_START, stage="Remuxing video & audio...")
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
            job_manager.update_job(job_id, status="processing", progress=PROCESS_END, stage="Finalizing container...")

    opts: Dict[str, Any] = {
        'outtmpl': out_template,
        **({'paths': {'home': str(staging)}} if staging else {}),
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
        # The same rule /api/info used to offer and price this quality
        # (app/services/formats.py): highest resolution within the cap, H.264
        # + AAC at that resolution.
        opts['format'] = video_format_spec(request.quality)
        opts['format_sort'] = VIDEO_FORMAT_SORT
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
                # As yt-dlp's own --convert-subs registers it. The default
                # ('post_process') never runs under skip_download, so the
                # .vtt was handed over unconverted.
                'when': 'before_dl',
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


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _is_scratch(path: Path) -> bool:
    """yt-dlp's in-progress and bookkeeping files, never a result."""
    return path.name.startswith(".") or path.suffix.lower() in (".part", ".ytdl", ".temp")


def _claim_free_name(directory: Path, name: str) -> Path:
    """Reserve ``name`` in ``directory``, or ``Stem (n).ext`` if it is taken.

    Reserved by exclusive creation, so two jobs finishing with the same title
    at the same moment cannot both pick the same name; the caller then
    replaces the empty placeholder with the real file.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(0, 10000):
        candidate = directory / (name if n == 0 else f"{stem} ({n}){suffix}")
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"No free name for {name} in {directory}")


def _publish_from_staging(path: Path, staging: Path, downloads: Path) -> Path:
    """Move a finished file out of the job's staging directory into Downloads.

    Never overwrites: an existing file of the same name is the user's, so the
    new one becomes ``Title (1).mp4`` and so on. A path already outside the
    staging directory is returned as is.
    """
    if not _is_within(path, staging):
        return path
    target = _claim_free_name(downloads, path.name)
    try:
        os.replace(path, target)
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return target


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
        """Remove the per-job scratch directory and whatever is left in it.

        Everything yt-dlp wrote lives there and the result has already been
        moved out, so the remainder is scratch: intermediates, a thumbnail
        that was embedded, subtitle files that were.
        """
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def purge_partials() -> None:
        """Remove every artifact this job produced, completed or not.

        On desktop the scratch directory is per-job, so this is exact: it can
        never touch a concurrent job's files. In hosted mode the job-id prefix
        provides the same isolation.
        """
        if staging_dir is not None:
            # Only paths inside our own staging directory: on desktop anything
            # outside it is the user's Downloads folder.
            candidates = {p for p in produced_files if _is_within(p, staging_dir)}
        else:
            candidates = set(produced_files)
            candidates.update(config.DOWNLOADS_DIR.glob(f"{job_id}_*"))

        for leftover in candidates:
            try:
                if leftover.is_file():
                    leftover.unlink()
            except OSError:
                pass
        _cleanup_staging_dir()

    try:
        await loop.run_in_executor(
            None, _sync_download, job_id, request, progress_port, produced_files
        )
        _raise_if_cancelled(job_id)

        # Locate completed files. Prefer what yt-dlp reported; fall back to a glob
        # only in hosted mode, where the job-id prefix is guaranteed.
        matching_files = [p for p in produced_files if p.exists()]
        if staging_dir is not None and staging_dir.is_dir():
            # A subtitle-only job reports no download at all -- yt-dlp's result
            # carries the converted .srt under requested_subtitles, not
            # filepath -- so the staging directory is the authoritative list.
            for f in sorted(staging_dir.iterdir()):
                if f.is_file() and f not in matching_files and not _is_scratch(f):
                    matching_files.append(f)
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
            elif f.suffix.lower() in [".mp4", ".mp3", ".m4a", ".webm", ".mkv"]:
                primary_file = f

        if not primary_file and srt_file:
            primary_file = srt_file

        if not primary_file:
            primary_file = matching_files[0]

        job_manager.update_job(job_id, status="processing", progress=SAVE_END, stage="Saving to Downloads...")
        if staging_dir is not None:
            moved = {}
            for f in {primary_file, srt_file} - {None}:
                moved[f] = _publish_from_staging(f, staging_dir, config.DOWNLOADS_DIR)
            primary_file = moved[primary_file]
            srt_file = moved.get(srt_file) if srt_file else None

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
        elif is_youtube_url(request.url) and is_upstream_refusal(raw):
            # The metadata probe can succeed while the media request is refused:
            # YouTube lets this address browse but not download, so an extract
            # says nothing about whether the transfer will work. Without this
            # branch the refusal surfaced as a generic "Download failed", the
            # circuit stayed closed, and the next attempt hit YouTube again --
            # which is exactly how repeated attempts escalate into the 429 the
            # user then reports. Trip the breaker and route to the same upsell
            # the landing page uses.
            youtube_circuit.record_failure()
            job_manager.update_job(
                job_id,
                status="error",
                error=(
                    "YouTube is blocking downloads from this server's address. "
                    "The Desktop and Android apps download from your own "
                    "connection, where this restriction doesn't apply."
                ),
                code="UPSTREAM_BLOCKED",
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
        # Success, cancel or failure alike: anything worth keeping has been
        # moved into Downloads by now, and the rest must not pile up as
        # .mediagrab-<id> folders there.
        _cleanup_staging_dir()
        if server:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
