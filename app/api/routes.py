import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import StreamingResponse, FileResponse

from app import config
from app.api.schemas import (
    CapabilitiesResponse,
    DownloadsResponse,
    InfoRequest,
    InfoResponse,
    DownloadRequest,
    DownloadResponse,
    JobProgressResponse,
    LimitsResponse,
)
from app.config import CLEANUP_GRACE_SECONDS, ORPHAN_MAX_AGE_SECONDS
from app.services.extractor import inspect_url
from app.services.jobs import job_manager
from app.services.downloader import execute_download
from app.services.cleaner import deferred_file_removal
from app.services.metadata import GuardrailError, get_cached_metadata, load_and_validate
from app.services.network import get_client_ip
from app.services.scheduler import PER_IP_LIMIT_MESSAGE, PerIPLimitExceeded, scheduler

router = APIRouter(prefix="/api")


def _release_urls() -> DownloadsResponse:
    base = config.GITHUB_RELEASES_URL.rstrip("/")
    return DownloadsResponse(
        windows_url=f"{base}/download/MediaGrab-Windows.exe",
        linux_url=f"{base}/download/MediaGrab-Linux-x86_64.tar.gz",
        android_url=f"{base}/download/MediaGrab-arm64-v8a.apk",
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def api_capabilities():
    """Single source of truth for what this deployment allows and offers."""
    hosted = config.IS_HOSTED
    limits = LimitsResponse(
        max_resolution=config.HOSTED_MAX_RESOLUTION if hosted else None,
        max_duration_seconds=config.HOSTED_MAX_DURATION_SECONDS if hosted else None,
        max_file_size_bytes=config.HOSTED_MAX_FILE_SIZE_BYTES if hosted else None,
        # Read the mode's own constants rather than the import-time-resolved
        # scheduler values, so this always describes the active policy.
        concurrency_limit=(config.MAX_CONCURRENT_JOBS if not hosted else config.HOSTED_CONCURRENCY) or None,
        per_ip_limit=(config.PER_IP_CONCURRENCY if not hosted else config.HOSTED_PER_IP_CONCURRENCY) or None,
        idle_watchdog_seconds=ORPHAN_MAX_AGE_SECONDS,
        locked_resolutions=list(config.HOSTED_LOCKED_RESOLUTIONS) if hosted else [],
    )
    return CapabilitiesResponse(
        mode="hosted" if hosted else "desktop",
        retention_seconds=CLEANUP_GRACE_SECONDS,
        limits=limits,
        downloads=_release_urls(),
    )


@router.post("/info", response_model=InfoResponse)
async def api_info(req: InfoRequest, request: Request):
    # Populate the metadata cache so /api/download can reuse this probe for
    # its guardrail checks instead of paying a second extraction.
    try:
        response = await inspect_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to inspect media URL: {str(e)}")

    get_cached_metadata().put(req.url, response)
    return response


@router.post("/download", response_model=DownloadResponse, status_code=202)
async def api_download(req: DownloadRequest, request: Request):
    client_ip = get_client_ip(request)

    # Reject over-quota clients before we allocate anything, so a repeat
    # attempt cannot consume a slot or leave a job record behind.
    try:
        await scheduler.check_per_ip(client_ip)
    except PerIPLimitExceeded:
        raise HTTPException(status_code=429, detail=PER_IP_LIMIT_MESSAGE)

    # Guardrails run against server-side metadata, never client-supplied values.
    try:
        await load_and_validate(req)
    except GuardrailError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to validate request: {str(e)}")

    job = job_manager.create_job(req)

    try:
        queue_position, waiter = await scheduler.acquire(job.id, client_ip)
    except PerIPLimitExceeded:
        # Lost a race with a concurrent request from the same IP.
        job_manager.update_job(job.id, status="error", error=PER_IP_LIMIT_MESSAGE)
        raise HTTPException(status_code=429, detail=PER_IP_LIMIT_MESSAGE)

    if queue_position == 0:
        job_manager.update_job(job.id, status="queued", active_slots=scheduler.active_slots)
    else:
        job_manager.update_job(
            job.id,
            status="queued",
            queue_position=queue_position,
            active_slots=scheduler.active_slots,
            stage=f"Server busy ({scheduler.active_slots}/{scheduler.concurrency} slots). "
                  f"Waiting in queue (Position {queue_position})...",
        )

    asyncio.create_task(_run_job(job.id, req, client_ip, waiter))

    return DownloadResponse(job_id=job.id)


async def _run_job(job_id: str, req: DownloadRequest, client_ip, waiter):
    """Drive a download, holding its scheduler slot for the whole run.

    A queued job awaits its waiter first; an immediately-granted one skips
    straight through. The slot is released on every exit path so a crash or an
    abort can never leak capacity.
    """
    try:
        if waiter is not None:
            await scheduler.wait_for_slot(waiter, job_id, client_ip)
            job_manager.update_job(
                job_id,
                queue_position=0,
                active_slots=scheduler.active_slots,
                stage=f"Starting download "
                      f"({scheduler.active_slots}/{scheduler.concurrency} slots in use)...",
            )
        await execute_download(job_id, req)
    except asyncio.CancelledError:
        job_manager.update_job(job_id, status="error", error=PER_IP_LIMIT_MESSAGE)
    finally:
        job_manager.update_job(job_id, active_slots=scheduler.active_slots)
        await scheduler.release(job_id, client_ip)
        if waiter is not None and not waiter.done():
            waiter.cancel()


@router.post("/abort/{job_id}")
async def api_abort(job_id: str):
    """Cancel an in-flight job and purge whatever it has written so far."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ("ready", "error", "cancelled"):
        return {"job_id": job_id, "status": job.status}

    job.request_cancel()
    # Remove it from the wait line so an aborted queued job stops occupying a
    # place in front of everyone else.
    await scheduler.cancel_waiting(job_id)
    job_manager.update_job(job_id, status="cancelled", stage="Download cancelled")
    return {"job_id": job_id, "status": "cancelled"}


@router.get("/progress/{job_id}", response_model=JobProgressResponse)
async def api_progress(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_response()


@router.get("/events/{job_id}")
async def api_events(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    queue = job_manager.subscribe(job_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Job queue unavailable")

    async def event_generator():
        try:
            while True:
                update: JobProgressResponse = await queue.get()

                if update.status == "ready":
                    data = {
                        "filename": update.filename,
                        "size": update.size,
                        "download_url": update.download_url,
                        "srt_filename": update.srt_filename,
                        "srt_url": update.srt_url
                    }
                    yield f"event: complete\ndata: {json.dumps(data)}\n\n"
                    break
                elif update.status == "error":
                    data = {"error": update.error or "Unknown failure"}
                    yield f"event: error\ndata: {json.dumps(data)}\n\n"
                    break
                elif update.status == "cancelled":
                    data = {"error": "Download cancelled"}
                    yield f"event: error\ndata: {json.dumps(data)}\n\n"
                    break
                else:
                    data = {
                        "status": update.status,
                        "progress": update.progress,
                        "speed": update.speed,
                        "eta": update.eta,
                        "got": update.got,
                        "stage": update.stage,
                        "queue_position": update.queue_position,
                        "active_slots": update.active_slots
                    }
                    yield f"event: progress\ndata: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            job_manager.unsubscribe(job_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/file/{job_id}")
async def api_file(job_id: str, background_tasks: BackgroundTasks, srt: bool = Query(False)):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="File expired or job not found")

    target_path: Optional[Path] = None
    target_name: Optional[str] = None

    if srt:
        target_path = job.srt_path
        target_name = job.srt_filename or "subtitles.srt"
    else:
        target_path = job.file_path
        target_name = job.filename or "media"

    if not target_path or not target_path.exists():
        raise HTTPException(status_code=404, detail="File not found on server")

    # The retention clock starts here -- on the first actual retrieval -- not
    # when the job became ready, so a file waiting on the ready screen is safe.
    # Marking it claimed also hands it over to the orphan watchdog.
    job_manager.update_job(job_id, claimed=True)
    background_tasks.add_task(
        deferred_file_removal, target_path, delay_seconds=CLEANUP_GRACE_SECONDS
    )

    media_type = "text/plain" if srt else "application/octet-stream"
    if target_name.endswith(".mp4"):
        media_type = "video/mp4"
    elif target_name.endswith(".mp3"):
        media_type = "audio/mpeg"

    return FileResponse(
        path=target_path,
        filename=target_name,
        media_type=media_type
    )