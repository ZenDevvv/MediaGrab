"""Dual-mode behavior: capabilities, guardrails, queueing, rate limiting, retention."""

import asyncio
import os
import time

import pytest
from httpx import AsyncClient, ASGITransport

from app import config
from app.api.schemas import DownloadRequest, InfoResponse, QualityOption, TrimConfig
from app.main import app
from app.services import metadata as metadata_mod
from app.services import network as network_mod
from app.services.cleaner import purge_orphan_files
from app.services.jobs import JobManager
from app.services.metadata import GuardrailError, estimate_size_bytes, resolve_duration, validate_request
from app.services.scheduler import DownloadScheduler, PER_IP_LIMIT_MESSAGE, PerIPLimitExceeded

GB = 1024 ** 3


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(config, "IS_HOSTED", True)


@pytest.fixture
def hosted_srt(monkeypatch):
    """Hosted mode with stubbed metadata, for tests that must reach the queue.

    In hosted mode /api/download validates before queueing, so these tests need
    a fake inspection to avoid a real network probe.
    """
    monkeypatch.setattr(config, "IS_HOSTED", True)

    async def fake_inspect(url):
        return make_metadata(duration=600)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    metadata_mod.get_cached_metadata().clear()
    yield
    metadata_mod.get_cached_metadata().clear()


@pytest.fixture
def behind_proxy(monkeypatch):
    """Enable trusted-header IP resolution so tests can address clients by IP."""
    monkeypatch.setattr(config, "BEHIND_PROXY", True)


@pytest.fixture(autouse=True)
def clean_state():
    """Keep the singleton job table from leaking between tests."""
    yield
    from app.services.jobs import job_manager
    job_manager._jobs.clear()


def make_metadata(
    duration: int = 600,
    qualities=None,
    audio_kbps: int = 128,
) -> InfoResponse:
    return InfoResponse(
        url="https://example.com/video",
        title="Test Video",
        uploader="Test",
        duration=duration,
        source_audio_kbps=audio_kbps,
        qualities=qualities if qualities is not None else [
            QualityOption(resolution="1080p", approx_bytes=int(0.4 * GB)),
            QualityOption(resolution="720p", approx_bytes=int(0.15 * GB)),
        ],
    )


# --- Capabilities ---------------------------------------------------------

@pytest.mark.asyncio
async def test_capabilities_desktop_mode(monkeypatch):
    monkeypatch.setattr(config, "IS_HOSTED", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "desktop"
    # Desktop is unlimited: no ceiling is advertised.
    assert body["limits"]["max_resolution"] is None
    assert body["limits"]["max_duration_seconds"] is None
    assert body["limits"]["max_file_size_bytes"] is None
    assert body["limits"]["locked_resolutions"] == []


@pytest.mark.asyncio
async def test_capabilities_hosted_mode_advertises_limits(monkeypatch, hosted):
    monkeypatch.setattr(config, "IS_HOSTED", True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "hosted"
    assert body["limits"]["max_resolution"] == "1080p"
    assert body["limits"]["max_duration_seconds"] == 3600
    assert body["limits"]["max_file_size_bytes"] == GB
    assert body["limits"]["concurrency_limit"] == 3
    assert body["limits"]["per_ip_limit"] == 1
    assert "2160p" in body["limits"]["locked_resolutions"]


@pytest.mark.asyncio
async def test_capabilities_release_urls_point_at_real_repo(monkeypatch):
    monkeypatch.setattr(config, "IS_HOSTED", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/capabilities")
    downloads = resp.json()["downloads"]
    for key in ("windows_url", "linux_url", "android_url"):
        assert "github.com/ZenDevvv/MediaGrab/releases/latest" in downloads[key]
    assert downloads["windows_url"].endswith(".exe")
    assert downloads["android_url"].endswith(".apk")


def test_watchdog_never_outruns_the_grace_window():
    # A watchdog shorter than the grace window could sweep a file before its
    # own scheduled purge, or race a file the user is about to save.
    assert config.ORPHAN_MAX_AGE_SECONDS >= config.CLEANUP_GRACE_SECONDS
    assert config.CLEANUP_GRACE_SECONDS > 0


# --- Real IP resolution ---------------------------------------------------

def test_ip_resolution_ignores_forwarded_headers_when_not_behind_proxy():
    resolved = network_mod.resolve_client_ip(
        cf_connecting_ip="1.1.1.1",
        x_forwarded_for="2.2.2.2, 3.3.3.3",
        peer_host="10.0.0.5",
        behind_proxy=False,
    )
    assert resolved == "10.0.0.5"


def test_ip_resolution_prefers_cloudflare_header_behind_proxy():
    resolved = network_mod.resolve_client_ip(
        cf_connecting_ip="1.1.1.1",
        x_forwarded_for="2.2.2.2",
        peer_host="10.0.0.5",
        behind_proxy=True,
    )
    assert resolved == "1.1.1.1"


def test_ip_resolution_uses_leftmost_forwarded_entry_behind_proxy():
    resolved = network_mod.resolve_client_ip(
        cf_connecting_ip=None,
        x_forwarded_for="2.2.2.2, 3.3.3.3, 4.4.4.4",
        peer_host="10.0.0.5",
        behind_proxy=True,
    )
    assert resolved == "2.2.2.2"


def test_ip_resolution_falls_back_to_peer_behind_proxy():
    resolved = network_mod.resolve_client_ip(None, None, "10.0.0.5", behind_proxy=True)
    assert resolved == "10.0.0.5"


# --- Clip-aware duration validation ---------------------------------------

def test_untrimmed_video_over_60m_rejected_in_hosted(hosted):
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    with pytest.raises(GuardrailError) as exc:
        validate_request(req, make_metadata(duration=7200))
    assert "60 minute hosted limit" in str(exc.value)
    assert "Trim a segment" in str(exc.value)


def test_trimmed_segment_within_limit_is_allowed_in_hosted(hosted):
    req = DownloadRequest(
        url="u", mode="mp4", quality="1080p",
        trim=TrimConfig(start=3600, end=6600),  # 50 minutes
    )
    validate_request(req, make_metadata(duration=10800))  # 3-hour source


def test_trimmed_segment_over_limit_is_rejected(hosted):
    req = DownloadRequest(
        url="u", mode="mp4", quality="1080p",
        trim=TrimConfig(start=0, end=5400),  # 90 minutes
    )
    with pytest.raises(GuardrailError) as exc:
        validate_request(req, make_metadata(duration=10800))
    assert "trim range is longer" in str(exc.value)


def test_srt_is_exempt_from_duration_limit(hosted):
    req = DownloadRequest(url="u", mode="srt")
    validate_request(req, make_metadata(duration=86400))  # 24-hour stream


def test_video_under_60m_allowed(hosted):
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    validate_request(req, make_metadata(duration=1800))


def test_duration_resolution_uses_trim_range_when_present():
    req = DownloadRequest(url="u", trim=TrimConfig(start=100, end=400))
    assert resolve_duration(req, make_metadata(duration=7200)) == 300

    req_untrimmed = DownloadRequest(url="u")
    assert resolve_duration(req_untrimmed, make_metadata(duration=7200)) == 7200


def test_desktop_mode_applies_no_guardrails(monkeypatch):
    monkeypatch.setattr(config, "IS_HOSTED", False)
    req = DownloadRequest(url="u", mode="mp4", quality="2160p", trim=TrimConfig(start=0, end=99999))
    validate_request(req, make_metadata(duration=86400))


# --- Resolution cap -------------------------------------------------------

@pytest.mark.parametrize("quality", ["1440p", "2160p"])
def test_high_resolutions_rejected_in_hosted(quality, hosted):
    req = DownloadRequest(url="u", mode="mp4", quality=quality)
    with pytest.raises(GuardrailError) as exc:
        validate_request(req, make_metadata(duration=600))
    assert "Desktop or Android app" in str(exc.value)


def test_1080p_allowed_in_hosted(hosted):
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    validate_request(req, make_metadata(duration=600))


# --- Size estimation and cap ---------------------------------------------

def test_oversized_estimate_rejected(hosted):
    big = [QualityOption(resolution="1080p", approx_bytes=int(1.6 * GB))]
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    with pytest.raises(GuardrailError) as exc:
        validate_request(req, make_metadata(duration=600, qualities=big))
    assert "1.60 GB" in str(exc.value)


def test_size_estimate_within_limit_allowed(hosted):
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    validate_request(req, make_metadata(duration=600))


def test_size_estimate_falls_back_to_smaller_option():
    # Requested quality missing from metadata; the largest option at or below
    # the cap is used rather than silently estimating nothing.
    qualities = [
        QualityOption(resolution="1080p", approx_bytes=int(0.8 * GB)),
        QualityOption(resolution="360p", approx_bytes=int(0.05 * GB)),
    ]
    req = DownloadRequest(url="u", mode="mp4", quality="480p")
    assert estimate_size_bytes(req, make_metadata(qualities=qualities)) == int(0.05 * GB)


def test_mp3_estimate_scales_with_bitrate():
    meta = make_metadata(duration=600, audio_kbps=128)
    req = DownloadRequest(url="u", mode="mp3", bitrate=192)
    expected = int(600 * 192 * 1000 / 8)
    assert estimate_size_bytes(req, meta) == expected


def test_mp3_estimate_shrinks_with_trim():
    meta = make_metadata(duration=6000, audio_kbps=128)
    req = DownloadRequest(url="u", mode="mp3", bitrate=192, trim=TrimConfig(start=0, end=600))
    assert estimate_size_bytes(req, meta) == int(600 * 192 * 1000 / 8)


def test_srt_estimate_is_zero():
    req = DownloadRequest(url="u", mode="srt")
    assert estimate_size_bytes(req, make_metadata(duration=99999)) == 0


def test_unknown_size_is_not_treated_as_oversized():
    # Zero means "unknown", which must pass pre-flight and rely on the
    # in-flight max-filesize guard instead of being rejected or waved through.
    req = DownloadRequest(url="u", mode="mp4", quality="1080p")
    meta = make_metadata(duration=600, qualities=[])
    assert estimate_size_bytes(req, meta) == 0
    validate_request(req, meta)


# --- Metadata cache -------------------------------------------------------

@pytest.mark.asyncio
async def test_metadata_cache_avoids_repeat_extraction(monkeypatch, hosted):
    calls = []

    async def fake_inspect(url):
        calls.append(url)
        return make_metadata(duration=600)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    cache = metadata_mod.get_cached_metadata()
    cache.clear()

    req = DownloadRequest(url="https://example.com/cached", mode="mp4", quality="1080p")
    await metadata_mod.load_and_validate(req)
    await metadata_mod.load_and_validate(req)
    assert len(calls) == 1
    cache.clear()


@pytest.mark.asyncio
async def test_metadata_cache_expires():
    cache = metadata_mod.get_cached_metadata()
    cache.clear()
    cache.put("https://example.com/x", make_metadata())
    assert cache.get("https://example.com/x") is not None

    # Backdate the entry past the TTL.
    entry = cache._entries["https://example.com/x"]
    entry.stored_at = time.time() - (config.METADATA_CACHE_TTL_SECONDS + 10)
    assert cache.get("https://example.com/x") is None
    cache.clear()


def test_metadata_cache_is_bounded():
    cache = metadata_mod._MetadataCache(ttl_seconds=300, max_entries=3)
    for i in range(10):
        cache.put(f"https://example.com/{i}", make_metadata())
    assert len(cache._entries) == 3
    # Most-recently-stored survives.
    assert cache.get("https://example.com/9") is not None


# --- Scheduler: global slots and FIFO -------------------------------------

@pytest.mark.asyncio
async def test_slots_are_granted_up_to_the_limit():
    sched = DownloadScheduler(concurrency=3, per_ip_limit=0)
    results = []
    for i in range(5):
        pos, waiter = await sched.acquire(f"job{i}", f"10.0.0.{i}")
        results.append((pos, waiter))
    assert results[0][0] == 0 and results[1][0] == 0 and results[2][0] == 0
    assert results[3][0] == 1 and results[4][0] == 2
    assert sched.active_slots == 3
    assert sched.waiting_count == 2


@pytest.mark.asyncio
async def test_released_slot_goes_to_first_waiter():
    sched = DownloadScheduler(concurrency=1, per_ip_limit=0)
    _, _ = await sched.acquire("job0", "10.0.0.1")
    pos1, waiter1 = await sched.acquire("job1", "10.0.0.2")
    pos2, waiter2 = await sched.acquire("job2", "10.0.0.3")
    assert (pos1, pos2) == (1, 2)

    await sched.release("job0", "10.0.0.1")
    await asyncio.wait_for(waiter1, timeout=1)
    assert waiter1.done() and not waiter2.done()

    await sched.release("job1", "10.0.0.2")
    await asyncio.wait_for(waiter2, timeout=1)
    assert waiter2.done()


@pytest.mark.asyncio
async def test_per_ip_limit_rejects_second_concurrent_job():
    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    pos, _ = await sched.acquire("job0", "203.0.113.9")
    assert pos == 0
    with pytest.raises(PerIPLimitExceeded) as exc:
        await sched.acquire("job1", "203.0.113.9")
    assert str(exc.value) == PER_IP_LIMIT_MESSAGE


@pytest.mark.asyncio
async def test_per_ip_limit_allows_different_ips():
    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    for i in range(3):
        pos, _ = await sched.acquire(f"job{i}", f"10.0.0.{i}")
        assert pos == 0


@pytest.mark.asyncio
async def test_per_ip_slot_is_freed_on_release():
    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    await sched.acquire("job0", "198.51.100.7")
    await sched.release("job0", "198.51.100.7")
    assert sched.active_for_ip("198.51.100.7") == 0
    # Now permitted again.
    pos, _ = await sched.acquire("job1", "198.51.100.7")
    assert pos == 0


@pytest.mark.asyncio
async def test_zero_limits_mean_unlimited():
    sched = DownloadScheduler(concurrency=0, per_ip_limit=0)
    for i in range(25):
        pos, _ = await sched.acquire(f"job{i}", f"10.0.0.{i}")
        assert pos == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_the_queue():
    sched = DownloadScheduler(concurrency=1, per_ip_limit=0)
    await sched.acquire("job0", "10.0.0.1")
    _, waiter = await sched.acquire("job1", "10.0.0.2")
    await sched.cancel_waiting("job1")
    assert sched.waiting_count == 0
    assert waiter.cancelled()


# --- HTTP: 429 rate limiting ---------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_ip_download_returns_429(monkeypatch, behind_proxy, hosted_srt):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/download",
            json={"url": "https://example.com/a", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.55"},
        )
        assert first.status_code == 202
        job_id = first.json()["job_id"]

        second = await client.post(
            "/api/download",
            json={"url": "https://example.com/b", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.55"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == PER_IP_LIMIT_MESSAGE

    # Abort to release the slot for other tests.
    await sched.cancel_waiting(job_id)
    await sched.release(job_id, None)


@pytest.mark.asyncio
async def test_hosted_duplicate_ip_returns_429_over_http(monkeypatch, behind_proxy, hosted_srt):
    """The rate limit must hold in hosted mode, where it actually applies."""
    from app.api import routes as routes_mod

    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/download",
            json={"url": "https://example.com/first", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert first.status_code == 202

        second = await client.post(
            "/api/download",
            json={"url": "https://example.com/second", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == PER_IP_LIMIT_MESSAGE

        # A different IP is unaffected by the first client's occupancy.
        third = await client.post(
            "/api/download",
            json={"url": "https://example.com/third", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.11"},
        )
        assert third.status_code == 202

    await sched.reset()


@pytest.mark.asyncio
async def test_uncapped_scheduler_never_queues():
    """Desktop mode ships a scheduler with both caps at 0 (unlimited).

    Limits are resolved at import time, so this asserts the desktop default is
    genuinely uncapped rather than relying on the ambient run mode.
    """
    from app.services.scheduler import DownloadScheduler

    uncapped = DownloadScheduler(concurrency=0, per_ip_limit=0)
    assert uncapped.is_limited() is False
    for i in range(50):
        pos, waiter = await uncapped.acquire(f"desktop-job-{i}", f"10.0.0.{i}")
        assert pos == 0 and waiter is None
    await uncapped.reset()


@pytest.mark.asyncio
async def test_distinct_ips_are_not_rate_limited(monkeypatch, behind_proxy, hosted_srt):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(concurrency=3, per_ip_limit=1)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for ip in ("10.0.0.1", "10.0.0.2"):
            resp = await client.post(
                "/api/download",
                json={"url": f"https://example.com/{ip}", "mode": "srt"},
                headers={"X-Forwarded-For": ip},
            )
            assert resp.status_code == 202
    await sched.reset()


# --- HTTP: guardrail rejection -------------------------------------------

@pytest.mark.asyncio
async def test_hosted_download_over_duration_is_rejected(monkeypatch):
    from app.api import routes as routes_mod

    monkeypatch.setattr(config, "IS_HOSTED", True)
    monkeypatch.setattr(routes_mod, "scheduler", DownloadScheduler(3, 1))

    async def fake_inspect(url):
        return make_metadata(duration=7200)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    metadata_mod.get_cached_metadata().clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/download", json={"url": "https://example.com/long", "mode": "mp4"})
        assert resp.status_code == 400
        assert "60 minute hosted limit" in resp.json()["detail"]
    metadata_mod.get_cached_metadata().clear()


@pytest.mark.asyncio
async def test_hosted_download_over_size_is_rejected(monkeypatch):
    from app.api import routes as routes_mod

    monkeypatch.setattr(config, "IS_HOSTED", True)
    monkeypatch.setattr(routes_mod, "scheduler", DownloadScheduler(3, 1))

    big = [QualityOption(resolution="1080p", approx_bytes=int(2 * GB))]

    async def fake_inspect(url):
        return make_metadata(duration=600, qualities=big)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    metadata_mod.get_cached_metadata().clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/download", json={"url": "https://example.com/big", "mode": "mp4"})
        assert resp.status_code == 400
        assert "hosted limit" in resp.json()["detail"]
    metadata_mod.get_cached_metadata().clear()


@pytest.mark.asyncio
async def test_hosted_trimmed_download_is_accepted(monkeypatch):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1)
    monkeypatch.setattr(config, "IS_HOSTED", True)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    async def fake_inspect(url):
        return make_metadata(duration=10800)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    metadata_mod.get_cached_metadata().clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/download",
            json={
                "url": "https://example.com/long",
                "mode": "mp4",
                "trim": {"start": 0, "end": 1200},
            },
        )
        assert resp.status_code == 202
    await sched.reset()


@pytest.mark.asyncio
async def test_hosted_srt_long_transcript_is_accepted(monkeypatch):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1)
    monkeypatch.setattr(config, "IS_HOSTED", True)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    async def fake_inspect(url):
        return make_metadata(duration=86400)

    monkeypatch.setattr(metadata_mod, "inspect_url", fake_inspect)
    metadata_mod.get_cached_metadata().clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/download", json={"url": "https://example.com/talk", "mode": "srt"})
        assert resp.status_code == 202
    await sched.reset()


# --- HTTP: abort ----------------------------------------------------------

@pytest.mark.asyncio
async def test_abort_marks_job_cancelled():
    from app.services.jobs import job_manager

    req = DownloadRequest(url="https://example.com/x", mode="mp4")
    job = job_manager.create_job(req)
    job_manager.update_job(job.id, status="downloading", stage="Downloading video")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/abort/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        progress = await client.get(f"/api/progress/{job.id}")
        assert progress.json()["status"] == "cancelled"
        # The worker thread must observe the cancellation flag.
        assert job.is_cancelled


@pytest.mark.asyncio
async def test_abort_unknown_job_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/abort/doesnotexist")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_abort_finished_job_is_idempotent():
    from app.services.jobs import job_manager

    job = job_manager.create_job(DownloadRequest(url="https://example.com/y", mode="mp4"))
    job_manager.update_job(job.id, status="ready")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/abort/{job.id}")
        assert resp.json()["status"] == "ready"


def test_abort_flag_causes_partial_purge(tmp_path, monkeypatch):
    from app.services import downloader as dl_mod

    job_manager = JobManager()
    monkeypatch.setattr(dl_mod, "job_manager", job_manager)
    monkeypatch.setattr(dl_mod, "DOWNLOADS_DIR", tmp_path)

    job = job_manager.create_job(DownloadRequest(url="https://example.com/z", mode="mp4"))
    (tmp_path / f"{job.id}_partial.mp4").write_text("half a file")
    job.request_cancel()

    with pytest.raises(dl_mod.DownloadCancelled):
        dl_mod._raise_if_cancelled(job.id)

    # Same helper the worker's exception path uses to clean up.
    for leftover in tmp_path.glob(f"{job.id}_*"):
        leftover.unlink()
    assert list(tmp_path.glob(f"{job.id}_*")) == []


# --- Retention ------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_purge_is_scheduled_from_get_file(monkeypatch):
    """The retention clock must start on retrieval, not on job completion."""
    from app.api import routes as routes_mod
    from app.services.jobs import job_manager
    from app.config import DOWNLOADS_DIR

    # ASGITransport runs BackgroundTasks inline, so shrink the real grace window
    # to assert the purge actually fires.
    monkeypatch.setattr(routes_mod, "CLEANUP_GRACE_SECONDS", 0.05)

    job = job_manager.create_job(DownloadRequest(url="https://example.com/f", mode="mp4"))
    target = DOWNLOADS_DIR / f"{job.id}_clip.mp4"
    target.write_text("payload")
    job_manager.update_job(job.id, status="ready", file_path=target, filename="clip.mp4")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/file/{job.id}")
        assert resp.status_code == 200
        assert resp.content == b"payload"
        # The scheduled purge has run by the time the response returns.
        assert not target.exists()

    # A second retrieval after the purge is a clean 404, not a 500.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        again = await client.get(f"/api/file/{job.id}")
        assert again.status_code == 404


@pytest.mark.asyncio
async def test_file_survives_while_sitting_on_ready_screen():
    """Regression: a file nobody has fetched yet must not be swept early."""
    from app.services.jobs import job_manager
    from app.config import DOWNLOADS_DIR

    job = job_manager.create_job(DownloadRequest(url="https://example.com/g", mode="mp4"))
    target = DOWNLOADS_DIR / f"{job.id}_ready.mp4"
    target.write_text("still waiting")
    job_manager.update_job(job.id, status="ready", file_path=target, filename="ready.mp4")

    # Backdate past the orphan threshold; the watchdog must still skip it
    # because an active job still references the file.
    old = time.time() - (config.ORPHAN_MAX_AGE_SECONDS * 3 + 100)
    os.utime(target, (old, old))

    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert target.exists()
    target.unlink()


def test_watchdog_sweeps_unclaimed_files():
    from app.config import DOWNLOADS_DIR

    orphan = DOWNLOADS_DIR / "unclaimed_orphan_test.mp4"
    orphan.write_text("nobody wants this")
    old = time.time() - (config.ORPHAN_MAX_AGE_SECONDS + 100)
    os.utime(orphan, (old, old))

    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert not orphan.exists()


def test_watchdog_sweeps_claimed_but_stale_files():
    """Once retrieved, a file ages out normally -- the ready screen no longer shields it."""
    from app.services.jobs import job_manager
    from app.config import DOWNLOADS_DIR

    job = job_manager.create_job(DownloadRequest(url="https://example.com/claimed", mode="mp4"))
    target = DOWNLOADS_DIR / f"{job.id}_claimed.mp4"
    target.write_text("user already saved this")
    job_manager.update_job(job.id, status="ready", file_path=target, filename="claimed.mp4")
    job_manager.update_job(job.id, claimed=True)

    old = time.time() - (config.ORPHAN_MAX_AGE_SECONDS + 100)
    os.utime(target, (old, old))

    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert not target.exists()


def test_watchdog_protects_in_flight_jobs():
    from app.services.jobs import job_manager
    from app.config import DOWNLOADS_DIR

    job = job_manager.create_job(DownloadRequest(url="https://example.com/live", mode="mp4"))
    partial = DOWNLOADS_DIR / f"{job.id}_partial.mp4"
    partial.write_text("still downloading")
    job_manager.update_job(job.id, status="downloading")

    old = time.time() - (config.ORPHAN_MAX_AGE_SECONDS * 2 + 100)
    os.utime(partial, (old, old))

    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert partial.exists()
    partial.unlink()


def test_watchdog_keeps_fresh_unclaimed_files():
    from app.config import DOWNLOADS_DIR

    fresh = DOWNLOADS_DIR / "fresh_orphan_test.mp4"
    fresh.write_text("just landed")
    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert fresh.exists()
    fresh.unlink()


# --- Job lifecycle --------------------------------------------------------

def test_job_ttl_comes_from_config(monkeypatch):
    monkeypatch.setattr(config, "JOB_TTL_SECONDS", 42)
    manager = JobManager()
    job = manager.create_job(DownloadRequest(url="https://example.com/ttl", mode="mp4"))
    assert job.expires_at - job.created_at == pytest.approx(42, abs=1)


def test_expiry_cancels_in_flight_jobs():
    manager = JobManager()
    job = manager.create_job(DownloadRequest(url="https://example.com/exp", mode="mp4"))
    manager.update_job(job.id, status="downloading")
    manager.clean_expired(current_time=time.time() + config.JOB_TTL_SECONDS + 10)
    assert job.is_cancelled
    assert manager.get_job(job.id) is None


def test_job_response_exposes_queue_fields():
    manager = JobManager()
    job = manager.create_job(DownloadRequest(url="https://example.com/q", mode="mp4"))
    manager.update_job(job.id, status="queued", queue_position=2, active_slots=3)
    resp = manager.get_job(job.id).to_response()
    assert resp.queue_position == 2
    assert resp.active_slots == 3