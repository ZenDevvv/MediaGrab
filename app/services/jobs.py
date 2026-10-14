import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app import config
from app.api.schemas import DownloadRequest, JobProgressResponse


@dataclass
class Job:
    id: str
    url: str
    request: DownloadRequest
    status: str = "queued"
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    got: Optional[str] = None
    stage: str = "Initializing"
    file_path: Optional[Path] = None
    filename: Optional[str] = None
    size: Optional[str] = None
    srt_path: Optional[Path] = None
    srt_filename: Optional[str] = None
    error: Optional[str] = None
    code: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + config.JOB_TTL_SECONDS)
    subscribers: List[asyncio.Queue] = field(default_factory=list)

    queue_position: int = 0
    active_slots: int = 0

    # Set once the file has actually been served. Until then the orphan
    # watchdog must leave it alone, even if it sits for a long time.
    claimed: bool = False

    # Cooperative cancellation flag shared with the worker thread.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def request_cancel(self) -> None:
        self.cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def to_response(self) -> JobProgressResponse:
        return JobProgressResponse(
            job_id=self.id,
            status=self.status,  # type: ignore[arg-type]
            progress=self.progress,
            speed=self.speed,
            eta=self.eta,
            got=self.got,
            stage=self.stage,
            filename=self.filename,
            size=self.size,
            download_url=f"/api/file/{self.id}" if self.status == "ready" else None,
            srt_filename=self.srt_filename,
            srt_url=f"/api/file/{self.id}?srt=true" if self.srt_path else None,
            error=self.error,
            code=self.code,
            queue_position=self.queue_position,
            active_slots=self.active_slots,
        )


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    def create_job(self, request: DownloadRequest) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job = Job(
            id=job_id,
            url=request.url,
            request=request,
        )
        self._jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def update_job(
        self,
        job_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        speed: Optional[str] = None,
        eta: Optional[str] = None,
        got: Optional[str] = None,
        stage: Optional[str] = None,
        file_path: Optional[Path] = None,
        filename: Optional[str] = None,
        size: Optional[str] = None,
        srt_path: Optional[Path] = None,
        srt_filename: Optional[str] = None,
        error: Optional[str] = None,
        code: Optional[str] = None,
        queue_position: Optional[int] = None,
        active_slots: Optional[int] = None,
        claimed: Optional[bool] = None,
    ):
        job = self._jobs.get(job_id)
        if not job:
            return

        if status is not None:
            job.status = status
        if progress is not None:
            # The bar only moves forward: phases report from different places
            # and a late report from an earlier one must not pull it back.
            job.progress = max(job.progress or 0.0, progress)
        if speed is not None:
            job.speed = speed
        if eta is not None:
            job.eta = eta
        if got is not None:
            job.got = got
        if stage is not None:
            job.stage = stage
        if file_path is not None:
            job.file_path = file_path
        if filename is not None:
            job.filename = filename
        if size is not None:
            job.size = size
        if srt_path is not None:
            job.srt_path = srt_path
        if srt_filename is not None:
            job.srt_filename = srt_filename
        if error is not None:
            job.error = error
        if code is not None:
            job.code = code
        if queue_position is not None:
            job.queue_position = queue_position
        if active_slots is not None:
            job.active_slots = active_slots
        if claimed is not None:
            job.claimed = claimed

        resp = job.to_response()
        for q in list(job.subscribers):
            try:
                q.put_nowait(resp)
            except asyncio.QueueFull:
                pass

    def broadcast(self, job_id: str):
        """Re-emit current state, e.g. when queue position changes."""
        job = self._jobs.get(job_id)
        if not job:
            return
        resp = job.to_response()
        for q in list(job.subscribers):
            try:
                q.put_nowait(resp)
            except asyncio.QueueFull:
                pass

    def subscribe(self, job_id: str) -> Optional[asyncio.Queue]:
        job = self._jobs.get(job_id)
        if not job:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        q.put_nowait(job.to_response())
        job.subscribers.append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        job = self._jobs.get(job_id)
        if job and q in job.subscribers:
            job.subscribers.remove(q)

    def clean_expired(self, current_time: Optional[float] = None):
        now = current_time or time.time()
        expired = [jid for jid, j in self._jobs.items() if now > j.expires_at]
        for jid in expired:
            job = self._jobs[jid]
            # Anything still streaming gets a cancellation signal before the
            # record disappears, otherwise the worker writes to a dead job.
            if job.status not in ("ready", "error", "cancelled"):
                job.request_cancel()
            del self._jobs[jid]


# Global singleton
job_manager = JobManager()