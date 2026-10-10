import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse, FileResponse
from app.api.schemas import (
    InfoRequest,
    InfoResponse,
    DownloadRequest,
    DownloadResponse,
    JobProgressResponse
)
from app.config import CLEANUP_GRACE_SECONDS
from app.services.extractor import inspect_url
from app.services.jobs import job_manager
from app.services.downloader import execute_download
from app.services.cleaner import deferred_file_removal

router = APIRouter(prefix="/api")

@router.post("/info", response_model=InfoResponse)
async def api_info(req: InfoRequest):
    try:
        return await inspect_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to inspect media URL: {str(e)}")

@router.post("/download", response_model=DownloadResponse, status_code=202)
async def api_download(req: DownloadRequest):
    job = job_manager.create_job(req)
    # Start asynchronous background download worker
    asyncio.create_task(execute_download(job.id, req))
    return DownloadResponse(job_id=job.id)

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
                else:
                    data = {
                        "status": update.status,
                        "progress": update.progress,
                        "speed": update.speed,
                        "eta": update.eta,
                        "got": update.got,
                        "stage": update.stage
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

    # Schedule deferred cleanup after download completes
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
