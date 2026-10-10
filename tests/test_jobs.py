import asyncio
import pytest
from app.api.schemas import DownloadRequest
from app.services.jobs import JobManager

@pytest.mark.asyncio
async def test_job_lifecycle_and_subscribers():
    mgr = JobManager()
    req = DownloadRequest(url="https://youtube.com/watch?v=demo", mode="mp4", quality="1080p")
    job = mgr.create_job(req)

    assert job.id is not None
    assert job.status == "queued"
    assert job.progress == 0.0

    # Test subscription queue
    queue = mgr.subscribe(job.id)
    assert queue is not None

    initial_msg = queue.get_nowait()
    assert initial_msg.status == "queued"
    assert initial_msg.job_id == job.id

    # Test update broadcast
    mgr.update_job(job.id, status="downloading", progress=45.5, speed="5.2 MB/s", stage="Downloading stream")

    updated_job = mgr.get_job(job.id)
    assert updated_job.status == "downloading"
    assert updated_job.progress == 45.5

    update_msg = queue.get_nowait()
    assert update_msg.status == "downloading"
    assert update_msg.progress == 45.5
    assert update_msg.speed == "5.2 MB/s"

    mgr.unsubscribe(job.id, queue)
    assert queue not in updated_job.subscribers

def test_job_expiration():
    mgr = JobManager()
    req = DownloadRequest(url="https://youtube.com/watch?v=demo")
    job = mgr.create_job(req)
    job.expires_at = 100.0  # In the past

    mgr.clean_expired(current_time=200.0)
    assert mgr.get_job(job.id) is None
