import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, Request, Response
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
from app.services.extractor import inspect_url, UpstreamBotCheck
from app.services.jobs import job_manager
from app.services.downloader import execute_download
from app.services.cleaner import deferred_file_removal
from app.services.downloads import release_urls
from app.services.logpaths import launcher_log_path
from app.services.youtube_circuit import circuit as youtube_circuit
from app.services.metadata import GuardrailError, get_cached_metadata, load_and_validate
from app.services.network import (
    Identity,
    issue_session_token,
    resolve_identity,
    session_key,
)
from app.services.scheduler import GateExceeded, scheduler

router = APIRouter(prefix="/api")


def _release_urls() -> DownloadsResponse:
    """Download targets for the native apps.

    Sourced from app.services.downloads so a deployment without published
    binaries never advertises a 404 (§1.4 Decision #16).
    """
    return DownloadsResponse(**release_urls())


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
        per_session_limit=(config.PER_SESSION_CONCURRENCY if not hosted else config.HOSTED_PER_SESSION_CONCURRENCY) or None,
        idle_watchdog_seconds=ORPHAN_MAX_AGE_SECONDS,
        locked_resolutions=list(config.HOSTED_LOCKED_RESOLUTIONS) if hosted else [],
    )
    return CapabilitiesResponse(
        mode="hosted" if hosted else "desktop",
        version=config.APP_VERSION,
        retention_seconds=CLEANUP_GRACE_SECONDS,
        limits=limits,
        downloads=_release_urls(),
    )


def _youtube_blocked_detail() -> dict:
    """The structured payload for a YouTube request the server declined to make.

    Wording is deliberately "most links" rather than "unavailable": the circuit
    allows the occasional working link through, so claiming impossibility would be
    wrong and would cost the user their trust the first time they verify
    elsewhere (§1.5 Decision #19).
    """
    return {
        "code": "UPSTREAM_BLOCKED",
        "message": (
            "YouTube limits requests from shared servers, so most links won't work "
            "here. The native app downloads from your own connection — no limits."
        ),
        "remedy": "native_app",
        "retry_after_seconds": int(youtube_circuit.remaining()),
    }


@router.post("/info", response_model=InfoResponse)
async def api_info(req: InfoRequest, request: Request):
    # Populate the metadata cache so /api/download can reuse this probe for
    # its guardrail checks instead of paying a second extraction.
    guarded = youtube_circuit.allows_request(req.url)
    if not guarded:
        raise HTTPException(status_code=451, detail=_youtube_blocked_detail())

    try:
        response = await inspect_url(req.url)
    except UpstreamBotCheck as e:
        youtube_circuit.record_failure()
        raise HTTPException(status_code=451, detail={
            "code": e.code,
            "message": str(e),
            "remedy": "native_app",
        })
    except ValueError as e:
        youtube_circuit.release_probe()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        youtube_circuit.release_probe()
        raise HTTPException(status_code=500, detail=f"Failed to inspect media URL: {str(e)}")

    youtube_circuit.record_success()
    get_cached_metadata().put(req.url, response)
    return response


@router.post("/download", response_model=DownloadResponse, status_code=202)
async def api_download(req: DownloadRequest, request: Request, response: Response):
    identity = resolve_identity(request)

    # Mint a session on first contact so subsequent requests are gated by
    # session rather than IP. Issued only in hosted mode; desktop is per-user
    # and uncapped, so a cookie would be pointless state.
    #
    # The freshly-minted token gates THIS request too: the client will hold it
    # on its next call, so gating on anything else would hand every new client an
    # ungated first download. The IP ceiling still bounds a cookie-dropping
    # client.
    gate_session = identity.session_id
    if config.IS_HOSTED and not identity.is_session_backed:
        minted = issue_session_token()
        # Gate on the same key a follow-up cookie will verify to, never on the
        # signed envelope -- otherwise the first and second requests occupy
        # different gate entries and the session gate never engages.
        gate_session = session_key(minted)
        response.set_cookie(
            config.SESSION_COOKIE_NAME,
            minted,
            max_age=config.SESSION_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    # Reject over-quota callers before allocating anything, so a repeat attempt
    # cannot consume a slot or leave a job record behind.
    try:
        await scheduler.check_gates(identity.session_id, identity.client_ip)
    except GateExceeded as gate:
        raise HTTPException(status_code=429, detail=gate.detail)

    # Guardrails run against server-side metadata, never client-supplied values.
    # load_and_validate may probe YouTube for a URL whose metadata is not cached,
    # so it goes through the same probe discipline as /api/info: this prevents a
    # burst of concurrent downloads from stampeding an address YouTube already
    # challenges (§1.5 Decision #17).
    if not youtube_circuit.allows_request(req.url):
        raise HTTPException(status_code=451, detail=_youtube_blocked_detail())

    try:
        await load_and_validate(req)
    except GuardrailError as e:
        youtube_circuit.release_probe()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except UpstreamBotCheck as e:
        youtube_circuit.record_failure()
        raise HTTPException(status_code=451, detail={
            "code": e.code,
            "message": str(e),
            "remedy": "native_app",
        })
    except ValueError as e:
        youtube_circuit.release_probe()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        youtube_circuit.release_probe()
        raise HTTPException(status_code=500, detail=f"Failed to validate request: {str(e)}")

    job = job_manager.create_job(req)
    youtube_circuit.record_success()

    try:
        queue_position, waiter = await scheduler.acquire(
            job.id, gate_session, identity.client_ip
        )
    except GateExceeded as gate:
        job_manager.update_job(job.id, status="error", error=gate.detail)
        raise HTTPException(status_code=429, detail=gate.detail)

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

    asyncio.create_task(_run_job(job.id, req, gate_session, identity.client_ip, waiter))

    return DownloadResponse(job_id=job.id)


async def _run_job(job_id: str, req: DownloadRequest, session_id, client_ip, waiter):
    """Drive a download, holding its scheduler slot for the whole run.

    A queued job awaits its waiter first; an immediately-granted one skips
    straight through. The slot is released on every exit path so a crash or an
    abort can never leak capacity.
    """
    try:
        if waiter is not None:
            await scheduler.wait_for_slot(waiter, job_id, session_id, client_ip)
            job_manager.update_job(
                job_id,
                queue_position=0,
                active_slots=scheduler.active_slots,
                stage=f"Starting download "
                      f"({scheduler.active_slots}/{scheduler.concurrency} slots in use)...",
            )
        await execute_download(job_id, req)
    except asyncio.CancelledError:
        job_manager.update_job(job_id, status="error", error="Download was cancelled.")
    finally:
        job_manager.update_job(job_id, active_slots=scheduler.active_slots)
        await scheduler.release(job_id, session_id, client_ip)
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


@router.post("/open-folder")
async def api_open_folder(request: Request):
    """Reveal the output directory in the OS file manager (desktop only).

    Hosted has no local filesystem to reveal, and there is no path a browser
    could be pointed at, so this refuses rather than pretending to succeed.
    """
    if config.IS_HOSTED:
        raise HTTPException(
            status_code=404,
            detail="Opening a local folder is only available in the desktop app.",
        )

    target = config.DOWNLOADS_DIR
    if not config.OS_OPEN_ENABLED:
        # The route is still exercised; only the OS side effect is suppressed,
        # so a test run cannot pop a file manager on the machine running it.
        return {"opened": str(target), "suppressed": True}

    try:
        _reveal_in_os(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open folder: {e}")

    return {"opened": str(target)}


# --- Diagnostics (§1.7) ---------------------------------------------------
# Desktop-only. These exist so a user can hand over something useful when the
# app misbehaves: the launcher log, which is the only record of a windowed
# build's internals, and whether a newer release exists.

#: Bytes of log returned to the clipboard. Enough for a recent failure without
#: putting a multi-megabyte file on the clipboard.
LOG_TAIL_BYTES = 200_000


def _require_desktop(feature: str) -> None:
    if config.IS_HOSTED:
        raise HTTPException(
            status_code=404,
            detail=f"{feature} is only available in the desktop app.",
        )


def _reveal_in_os(path: Path) -> None:
    """Open ``path`` with whatever the OS associates with it."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def _parse_version(raw: str) -> tuple:
    """Best-effort semver tuple. Unparseable input sorts lowest, not highest.

    Returning ``(0,)`` for junk matters: a release tag we cannot read must
    never be reported as newer than what the user is running.
    """
    cleaned = raw.strip().lstrip("vV").split("+")[0]
    parts = []
    for chunk in cleaned.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


@router.get("/diagnostics/log")
async def api_diagnostics_log(tail_bytes: int = Query(LOG_TAIL_BYTES, ge=1024, le=5_000_000)):
    """Return the launcher log tail and where the file lives.

    Only the tail is returned: the file grows without bound and a failed run is
    always at the end.
    """
    _require_desktop("The launcher log")

    path = launcher_log_path()
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "content": "",
            "message": "No log file yet. It is created the first time the app starts.",
        }

    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
        content = data.decode("utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not read the log: {e}")

    return {
        "path": str(path),
        "exists": True,
        "truncated": size > tail_bytes,
        "content": content,
    }


@router.post("/diagnostics/open-log")
async def api_diagnostics_open_log():
    """Reveal launcher.log in the OS default application."""
    _require_desktop("Opening the log file")

    path = launcher_log_path()
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No log file yet. Start the app once and try again.",
        )
    if not config.OS_OPEN_ENABLED:
        # Same reasoning as /open-folder: exercising the route must not launch
        # an editor on the machine running the suite.
        return {"opened": str(path), "suppressed": True}

    try:
        _reveal_in_os(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open the log: {e}")
    return {"opened": str(path)}


@router.get("/diagnostics/update-check")
async def api_diagnostics_update_check():
    """Compare the running build against the newest GitHub release.

    Goes through the server rather than the browser so it works identically in
    the packaged app, where the page is served from a loopback address and the
    release API would be a cross-origin request.
    """
    _require_desktop("Update checks")

    current = config.APP_VERSION
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest"
    try:
        import httpx

        response = httpx.get(
            url,
            timeout=10.0,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        # A failed check must not look like "no update": say we could not tell.
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach GitHub to check for updates: {e}",
        )

    latest_tag = payload.get("tag_name") or "0"
    html_url = payload.get("html_url") or f"https://github.com/{config.GITHUB_REPO}/releases/latest"

    if current in ("dev", "", None):
        return {
            "current": current,
            "latest": latest_tag,
            "update_available": False,
            "release_url": html_url,
            "checked": True,
            "note": "Development build; version comparison is skipped.",
        }

    update_available = _parse_version(latest_tag) > _parse_version(current)
    return {
        "current": current,
        "latest": latest_tag,
        "update_available": update_available,
        "release_url": html_url,
        "checked": True,
    }


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