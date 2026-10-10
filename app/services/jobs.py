import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
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
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 1800)
    subscribers: List[asyncio.Queue] = field(default_factory=list)

    def to_response(self) -> JobProgressResponse:
        return JobProgressResponse(
            job_id=self.id,
            status=self.status, # type: ignore
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
            error=self.error
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
            request=request
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
        error: Optional[str] = None
    ):
        job = self._jobs.get(job_id)
        if not job:
            return

        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
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

        # Broadcast update to subscribers
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
        # Push initial state
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
            del self._jobs[jid]

# Global singleton
job_manager = JobManager()
