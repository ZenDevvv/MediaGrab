"""Dual-mode behavior: capabilities, guardrails, queueing, rate limiting, retention."""

import asyncio
import os
import time

from pathlib import Path

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
from app.services.network import issue_session_token, verify_session_token
from app.services.scheduler import (
    GateExceeded,
    PER_IP_LIMIT_MESSAGE,
    PER_SESSION_LIMIT_MESSAGE,
    DownloadScheduler,
    PerIPLimitExceeded,
    PerSessionLimitExceeded,
)

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
def retention_enabled(monkeypatch):
    """Assert hosted retention semantics.

    Desktop mode disables deletion entirely (§1.3 Decision #12); these tests
    cover the sweep itself, so retention is switched on explicitly.
    """
    monkeypatch.setattr(config, "RETENTION_ENABLED", True)


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
    assert body["limits"]["per_session_limit"] == 1
    assert body["limits"]["per_ip_limit"] == 3
    assert "2160p" in body["limits"]["locked_resolutions"]


@pytest.mark.asyncio
async def test_download_targets_never_point_at_a_404(monkeypatch):
    """§1.4 Decision #16: an unpublished deployment must not advertise dead links.

    GitHub's /releases/latest/download/ path 404s until a release exists, so
    until then every target must resolve to the coming-soon page instead.
    """
    from app.services import downloads as dl

    monkeypatch.setattr(dl, "DOWNLOADS_AVAILABLE", False)
    urls = dl.release_urls()
    assert set(urls) == {"windows_url", "linux_url", "android_url"}
    for key, url in urls.items():
        assert url.startswith("/"), f"{key} would 404: {url}"


def test_download_targets_switch_to_real_assets_when_published(monkeypatch):
    from app.services import downloads as dl

    monkeypatch.setattr(dl, "DOWNLOADS_AVAILABLE", True)
    urls = dl.release_urls()

    # Asset names must match what build-desktop.yml actually attaches.
    assert urls["windows_url"].endswith("MediaGrab-Windows.zip")
    assert urls["linux_url"].endswith("MediaGrab-Linux-x86_64.tar.gz")
    for key in ("windows_url", "linux_url"):
        assert "github.com/ZenDevvv/MediaGrab/releases/latest/download/" in urls[key], urls[key]

    # CI has no Android job, so there is no APK to point at. Asserting one here
    # would have preserved the very dead link this module exists to prevent.
    assert urls["android_url"] == dl.coming_soon_url()


@pytest.mark.asyncio
async def test_capabilities_exposes_download_targets(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    downloads_body = resp.json()["downloads"]
    for key in ("windows_url", "linux_url", "android_url"):
        assert downloads_body[key], f"{key} must never be empty"


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
    sched = DownloadScheduler(concurrency=3, per_session_limit=0, per_ip_limit=0)
    results = []
    for i in range(5):
        pos, waiter = await sched.acquire(f"job{i}", None, f"10.0.0.{i}")
        results.append((pos, waiter))
    assert results[0][0] == 0 and results[1][0] == 0 and results[2][0] == 0
    assert results[3][0] == 1 and results[4][0] == 2
    assert sched.active_slots == 3
    assert sched.waiting_count == 2


@pytest.mark.asyncio
async def test_released_slot_goes_to_first_waiter():
    sched = DownloadScheduler(concurrency=1, per_session_limit=0, per_ip_limit=0)
    await sched.acquire("job0", None, "10.0.0.1")
    pos1, waiter1 = await sched.acquire("job1", None, "10.0.0.2")
    pos2, waiter2 = await sched.acquire("job2", None, "10.0.0.3")
    assert (pos1, pos2) == (1, 2)

    await sched.release("job0", None, "10.0.0.1")
    await asyncio.wait_for(waiter1, timeout=1)
    assert waiter1.done() and not waiter2.done()

    await sched.release("job1", None, "10.0.0.2")
    await asyncio.wait_for(waiter2, timeout=1)
    assert waiter2.done()


@pytest.mark.asyncio
async def test_zero_limits_mean_unlimited():
    sched = DownloadScheduler(concurrency=0, per_session_limit=0, per_ip_limit=0)
    for i in range(25):
        pos, _ = await sched.acquire(f"job{i}", None, f"10.0.0.{i}")
        assert pos == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_the_queue():
    sched = DownloadScheduler(concurrency=1, per_session_limit=0, per_ip_limit=0)
    await sched.acquire("job0", None, "10.0.0.1")
    _, waiter = await sched.acquire("job1", None, "10.0.0.2")
    await sched.cancel_waiting("job1")
    assert sched.waiting_count == 0
    assert waiter.cancelled()


# --- Session gate and IP ceiling (§1.1 Decision #2) -----------------------

@pytest.mark.asyncio
async def test_session_gate_rejects_second_job_from_same_session():
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=3)
    pos, _ = await sched.acquire("job0", "sess-a", "10.0.0.1")
    assert pos == 0
    with pytest.raises(PerSessionLimitExceeded) as exc:
        await sched.acquire("job1", "sess-a", "10.0.0.1")
    assert str(exc.value) == PER_SESSION_LIMIT_MESSAGE
    assert exc.value.gate == "session"


@pytest.mark.asyncio
async def test_two_sessions_behind_one_nat_are_both_allowed():
    """The CGNAT case: unrelated users sharing an egress IP must not collide."""
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=3)
    shared_ip = "203.0.113.77"
    pos_a, _ = await sched.acquire("job-a", "sess-a", shared_ip)
    pos_b, _ = await sched.acquire("job-b", "sess-b", shared_ip)
    assert pos_a == 0 and pos_b == 0
    assert sched.active_slots == 2


@pytest.mark.asyncio
async def test_ip_ceiling_stops_cookie_dropping():
    """Dropping the cookie must not let one client exceed the IP ceiling."""
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=2)
    shared_ip = "203.0.113.88"
    await sched.acquire("job0", "sess-a", shared_ip)
    await sched.acquire("job1", "sess-b", shared_ip)
    # A third "new session" on the same IP is refused by the ceiling.
    with pytest.raises(PerIPLimitExceeded) as exc:
        await sched.acquire("job2", "sess-c", shared_ip)
    assert str(exc.value) == PER_IP_LIMIT_MESSAGE
    assert exc.value.gate == "ip"


@pytest.mark.asyncio
async def test_session_gate_never_exceeds_ip_ceiling():
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=3)
    for i in range(3):
        await sched.acquire(f"job{i}", f"sess-{i}", "10.0.0.5")
    with pytest.raises(GateExceeded):
        await sched.acquire("job3", "sess-9", "10.0.0.5")


@pytest.mark.asyncio
async def test_session_slot_freed_on_release():
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=3)
    await sched.acquire("job0", "sess-x", "10.0.0.1")
    assert sched.active_for_session("sess-x") == 1
    await sched.release("job0", "sess-x", "10.0.0.1")
    assert sched.active_for_session("sess-x") == 0
    assert sched.active_for_ip("10.0.0.1") == 0
    pos, _ = await sched.acquire("job1", "sess-x", "10.0.0.1")
    assert pos == 0


@pytest.mark.asyncio
async def test_check_gates_reports_without_allocating():
    sched = DownloadScheduler(concurrency=3, per_session_limit=1, per_ip_limit=3)
    await sched.acquire("job0", "sess-a", "10.0.0.1")
    with pytest.raises(PerSessionLimitExceeded):
        await sched.check_gates("sess-a", "10.0.0.9")
    # Nothing was reserved by the failed check.
    assert sched.active_slots == 1


@pytest.mark.asyncio
async def test_aborting_a_queued_job_releases_its_gate_slot():
    """An aborted queued job must not keep occupying its session allowance."""
    sched = DownloadScheduler(concurrency=1, per_session_limit=1, per_ip_limit=3)
    await sched.acquire("job0", "sess-a", "10.0.0.1")
    _, waiter = await sched.acquire("job1", "sess-b", "10.0.0.2")
    await sched.cancel_waiting("job1")
    assert sched.active_for_session("sess-b") == 0
    assert sched.active_for_ip("10.0.0.2") == 0


@pytest.fixture
def hold_jobs(monkeypatch):
    """Keep background download jobs in-flight so gate state is deterministic.

    Without this, a job whose URL fails fast releases its slot before the next
    request arrives, and the gate tests race the worker.
    """
    from app.api import routes as routes_mod

    release = asyncio.Event()

    async def fake_execute(job_id, request):
        await release.wait()

    monkeypatch.setattr(routes_mod, "execute_download", fake_execute)
    yield release
    release.set()


# --- HTTP: 429 rate limiting and sessions ---------------------------------

@pytest.mark.asyncio
async def test_hosted_mints_a_session_cookie_on_first_download(monkeypatch, hosted_srt, hold_jobs):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1, 3)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/download",
            json={"url": "https://example.com/a", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert resp.status_code == 202
        # HttpOnly keeps it out of JS; SameSite=Lax blocks cross-site use.
        set_cookie = resp.headers.get("set-cookie", "")
        assert config.SESSION_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")
        # httpx persists it, so the next call is session-backed.
        assert any(c.name == config.SESSION_COOKIE_NAME for c in client.cookies.jar)
    await sched.reset()


@pytest.mark.asyncio
async def test_same_session_second_download_returns_429(monkeypatch, behind_proxy, hosted_srt, hold_jobs):
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1, 3)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    headers = {"X-Forwarded-For": "203.0.113.55"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/download",
            json={"url": "https://example.com/a", "mode": "srt"},
            headers=headers,
        )
        assert first.status_code == 202

        second = await client.post(
            "/api/download",
            json={"url": "https://example.com/b", "mode": "srt"},
            headers=headers,
        )
        assert second.status_code == 429
        assert second.json()["detail"] == PER_SESSION_LIMIT_MESSAGE
    await sched.reset()


@pytest.mark.asyncio
async def test_two_sessions_behind_one_ip_are_both_accepted_over_http(
    monkeypatch, behind_proxy, hosted_srt
, hold_jobs):
    """Regression: users sharing a CGNAT NAT must not 429 each other."""
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1, 3)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    shared_ip = {"X-Forwarded-For": "203.0.113.99"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as alice:
        r1 = await alice.post("/api/download", json={"url": "https://example.com/1", "mode": "srt"}, headers=shared_ip)
        assert r1.status_code == 202

    # A separate client (separate cookie jar) is a separate session.
    async with AsyncClient(transport=transport, base_url="http://test") as bob:
        r2 = await bob.post("/api/download", json={"url": "https://example.com/2", "mode": "srt"}, headers=shared_ip)
        assert r2.status_code == 202
    await sched.reset()


@pytest.mark.asyncio
async def test_ip_ceiling_over_http(monkeypatch, behind_proxy, hosted_srt, hold_jobs):
    """Fresh sessions from one IP still hit the ceiling."""
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1, 2)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    shared_ip = {"X-Forwarded-For": "203.0.113.100"}
    transport = ASGITransport(app=app)
    statuses = []
    for i in range(3):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/download",
                json={"url": f"https://example.com/{i}", "mode": "srt"},
                headers=shared_ip,
            )
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                assert resp.json()["detail"] == PER_IP_LIMIT_MESSAGE
    assert statuses == [202, 202, 429]
    await sched.reset()


@pytest.mark.asyncio
async def test_tampered_cookie_is_rejected_and_regenerated(monkeypatch, behind_proxy, hosted_srt, hold_jobs):
    """A forged signature must not buy a fresh gate."""
    from app.api import routes as routes_mod

    sched = DownloadScheduler(3, 1, 3)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    headers = {"X-Forwarded-For": "203.0.113.60", "Cookie": f"{config.SESSION_COOKIE_NAME}=forged.deadbeef"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/download",
            json={"url": "https://example.com/a", "mode": "srt"},
            headers=headers,
        )
        # Falls back to IP gating rather than trusting the forged identity.
        assert resp.status_code == 202
        assert verify_session_token("forged.deadbeef") is None
    await sched.reset()


@pytest.mark.asyncio
async def test_desktop_mode_issues_no_session_cookie(monkeypatch, behind_proxy, hosted_srt, hold_jobs):
    """Desktop is per-user and uncapped; a cookie would be pointless state."""
    from app.api import routes as routes_mod

    monkeypatch.setattr(config, "IS_HOSTED", False)
    sched = DownloadScheduler(0, 0, 0)
    monkeypatch.setattr(routes_mod, "scheduler", sched)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/download",
            json={"url": "https://example.com/local", "mode": "srt"},
            headers={"X-Forwarded-For": "203.0.113.70"},
        )
        assert resp.status_code == 202
        assert config.SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")
    await sched.reset()


# --- HTTP: guardrail rejection -------------------------------------------

@pytest.mark.asyncio
async def test_hosted_download_over_duration_is_rejected(monkeypatch):
    from app.api import routes as routes_mod

    monkeypatch.setattr(config, "IS_HOSTED", True)
    monkeypatch.setattr(routes_mod, "scheduler", DownloadScheduler(3, 1, 3))

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
    monkeypatch.setattr(routes_mod, "scheduler", DownloadScheduler(3, 1, 3))

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

    sched = DownloadScheduler(3, 1, 3)
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

    sched = DownloadScheduler(3, 1, 3)
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
async def test_file_purge_is_scheduled_from_get_file(monkeypatch, retention_enabled):
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


def test_watchdog_sweeps_unclaimed_files(retention_enabled):
    from app.config import DOWNLOADS_DIR

    orphan = DOWNLOADS_DIR / "unclaimed_orphan_test.mp4"
    orphan.write_text("nobody wants this")
    old = time.time() - (config.ORPHAN_MAX_AGE_SECONDS + 100)
    os.utime(orphan, (old, old))

    purge_orphan_files(max_age_seconds=config.ORPHAN_MAX_AGE_SECONDS)
    assert not orphan.exists()


def test_watchdog_sweeps_claimed_but_stale_files(retention_enabled):
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


def test_watchdog_keeps_fresh_unclaimed_files(retention_enabled):
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

# --- Upstream bot-check handling ------------------------------------------

def test_upstream_botcheck_is_distinct_from_a_bad_url():
    from app.services.extractor import UpstreamBotCheck
    import yt_dlp

    assert issubclass(UpstreamBotCheck, ValueError)
    assert UpstreamBotCheck.code == "UPSTREAM_BLOCKED"


@pytest.mark.asyncio
async def test_botcheck_returns_451_with_native_app_remedy(monkeypatch):
    """A blocked upstream address must not look like a malformed URL."""
    from app.api import routes as routes_mod
    from app.services.extractor import UpstreamBotCheck

    async def blocked(url):
        raise UpstreamBotCheck("YouTube is blocking requests from this server's address.")

    monkeypatch.setattr(routes_mod, "inspect_url", blocked)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/api/info", json={"url": "https://youtube.com/watch?v=x"})

    assert resp.status_code == 451
    detail = resp.json()["detail"]
    assert detail["code"] == "UPSTREAM_BLOCKED"
    assert detail["remedy"] == "native_app"
    assert "blocking" in detail["message"]


def test_frontend_routes_451_to_the_upsell_not_the_error_screen():
    with open("static/js/app.js", "r", encoding="utf-8") as f:
        js = f.read()
    assert "showYouTubeUpsell" in js
    assert "resp.status === 451" in js
    assert "detailPayload" in js
    # The superseded error-line handler must not linger as a second path.
    assert "showUpstreamBlocked" not in js


# --- YouTube circuit breaker (§1.5 Decision #17) --------------------------

def test_circuit_recognises_youtube_urls_and_ignores_lookalikes():
    from app.services.youtube_circuit import is_youtube_url

    assert is_youtube_url("https://www.youtube.com/watch?v=abc")
    assert is_youtube_url("https://youtu.be/abc")
    assert is_youtube_url("https://m.youtube.com/shorts/abc")
    assert is_youtube_url("https://music.youtube.com/watch?v=abc")
    assert is_youtube_url("youtube.com/watch?v=abc")

    # Lookalikes and other platforms must not trip the breaker.
    assert not is_youtube_url("https://notyoutube.com/watch?v=abc")
    assert not is_youtube_url("https://vimeo.com/123")
    assert not is_youtube_url("")
    assert not is_youtube_url("https://example.com/youtube.com")


def test_circuit_blocks_youtube_only_and_opens_after_failure():
    from app.services.youtube_circuit import YouTubeCircuit

    c = YouTubeCircuit(cooldown_seconds=1800)
    assert c.allows_request("https://vimeo.com/123") is True

    # First YouTube probe is allowed through.
    assert c.allows_request("https://youtube.com/watch?v=a") is True
    # A concurrent request must not stampede the single allowed probe.
    assert c.allows_request("https://youtube.com/watch?v=b") is False

    c.record_failure()
    assert c.is_open() is True
    assert c.remaining() > 1790
    # While open, no request reaches YouTube, regardless of traffic.
    assert c.allows_request("https://youtube.com/watch?v=c") is False
    # Other sites are unaffected by the breaker.
    assert c.allows_request("https://vimeo.com/123") is True


def test_circuit_closes_after_cooldown_and_allows_one_more_probe():
    from app.services.youtube_circuit import YouTubeCircuit

    c = YouTubeCircuit(cooldown_seconds=1800)
    now = 1000.0
    assert c.allows_request("https://youtube.com/watch?v=a", now=now) is True
    c.record_failure(now=now)

    # Still open just before expiry.
    assert c.is_open(now=now + 1799) is True
    # Closes exactly at expiry, releasing one probe.
    assert c.is_open(now=now + 1800) is False
    assert c.allows_request("https://youtube.com/watch?v=a", now=now + 1800) is True
    assert c.allows_request("https://youtube.com/watch?v=b", now=now + 1800) is False


def test_successful_probe_closes_the_circuit():
    from app.services.youtube_circuit import YouTubeCircuit

    c = YouTubeCircuit(cooldown_seconds=1800)
    now = 1000.0
    assert c.allows_request("https://youtube.com/watch?v=a", now=now) is True
    c.record_failure(now=now)
    assert c.is_open(now=now + 1) is True

    # Once the window elapses a probe succeeds: the circuit stays closed.
    assert c.allows_request("https://youtube.com/watch?v=a", now=now + 1800) is True
    c.record_success(now=now + 1800)
    assert c.is_open(now=now + 1801) is False
    # Unblocked traffic flows normally, serially.
    assert c.allows_request("https://youtube.com/watch?v=x", now=now + 1802) is True
    c.record_success(now=now + 1802)
    assert c.allows_request("https://youtube.com/watch?v=y", now=now + 1803) is True


def test_release_probe_frees_the_slot_without_tripping():
    from app.services.youtube_circuit import YouTubeCircuit

    c = YouTubeCircuit(cooldown_seconds=1800)
    assert c.allows_request("https://youtube.com/watch?v=a") is True
    assert c.allows_request("https://youtube.com/watch?v=b") is False
    # An internal error must not strand the slot or open the circuit.
    c.release_probe()
    assert c.is_open() is False
    assert c.allows_request("https://youtube.com/watch?v=b") is True


@pytest.mark.asyncio
async def test_info_does_not_reach_youtube_while_the_circuit_is_open(monkeypatch):
    """The whole point: no outbound request while the breaker is open."""
    from app.api import routes as routes_mod
    from app.services.youtube_circuit import circuit

    calls = []

    async def spy(url):
        calls.append(url)
        raise AssertionError("YouTube must not be contacted while the circuit is open")

    monkeypatch.setattr(routes_mod, "inspect_url", spy)
    monkeypatch.setattr(routes_mod.youtube_circuit, "is_open", lambda *a: True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/api/info", json={"url": "https://youtube.com/watch?v=x"})

    assert calls == []
    assert resp.status_code == 451
    detail = resp.json()["detail"]
    assert detail["code"] == "UPSTREAM_BLOCKED"
    assert detail["remedy"] == "native_app"
    # Copy must stay truthful: "most links", not "unavailable" (Decision #19).
    assert "most links" in detail["message"]
    assert "unavailable" not in detail["message"].lower()


def test_default_cooldown_is_thirty_minutes():
    from app import config

    assert config.YOUTUBE_COOLDOWN_SECONDS == 1800


# --- Per-platform download availability ------------------------------------

def test_unpublished_platforms_never_get_a_release_url(monkeypatch):
    """CI builds Windows and Linux but has no Android job.

    An all-or-nothing switch would advertise an APK that does not exist --
    the dead link §1.4 Decision #16 exists to prevent.
    """
    from app.services import downloads

    monkeypatch.setattr(downloads, "DOWNLOADS_AVAILABLE", True)
    urls = downloads.release_urls()

    assert urls["windows_url"].endswith("/MediaGrab-Windows.zip")
    assert urls["linux_url"].endswith("/MediaGrab-Linux-x86_64.tar.gz")
    # No APK is built, so Android must not advertise one.
    assert urls["android_url"] == downloads.coming_soon_url()
    assert ".apk" not in urls["android_url"]


def test_release_urls_respect_the_published_platform_list(monkeypatch):
    from app.services import downloads

    monkeypatch.setattr(downloads, "DOWNLOADS_AVAILABLE", True)
    monkeypatch.setattr(downloads, "PUBLISHED_PLATFORMS", frozenset({"linux"}))

    urls = downloads.release_urls()
    assert urls["linux_url"].endswith("/MediaGrab-Linux-x86_64.tar.gz")
    assert urls["windows_url"] == downloads.coming_soon_url()


def test_no_release_urls_before_binaries_are_published(monkeypatch):
    from app.services import downloads

    monkeypatch.setattr(downloads, "DOWNLOADS_AVAILABLE", False)
    for url in downloads.release_urls().values():
        assert url == downloads.coming_soon_url()
        assert "github.com" not in url


def test_android_is_unpublished_by_default():
    """Guards against an APK appearing in CI without the flag being updated."""
    from app.services.downloads import PUBLISHED_PLATFORMS

    assert "android" not in PUBLISHED_PLATFORMS
    assert {"windows", "linux"} <= PUBLISHED_PLATFORMS


# --- Upstream refusal recognition (§ "too many requests" report) ----------
# YouTube answers a refused datacenter address in several ways. Matching only
# the bot check left 403 and 429 as generic errors, so the circuit stayed
# closed and every retry hit YouTube again -- which is how attempts escalate
# into the very 429 the user reported.

def test_refusal_detection_covers_every_shape_youtube_uses():
    from app.services.extractor import is_upstream_refusal

    for message in (
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "HTTP Error 429: Too Many Requests",
        "HTTP Error 429",
    ):
        assert is_upstream_refusal(message), message

    # Genuinely bad links must not be mistaken for an address problem.
    # Geo-restriction in particular is a content decision, not anti-bot: sending
    # the user to the native app would be advice that cannot help.
    for message in (
        "Unsupported URL",
        "Private video",
        "Video unavailable",
        "ERROR: The uploader has not made this video available in your country",
        "",
    ):
        assert not is_upstream_refusal(message), message


@pytest.mark.asyncio
async def test_refusal_only_trips_the_breaker_for_youtube(monkeypatch):
    """A 403 on another host may just mean the link is gone."""
    from app.api import routes as routes_mod
    from app.services.extractor import UpstreamBotCheck
    import yt_dlp

    calls = []

    async def blocked(url):
        calls.append(url)
        raise yt_dlp.utils.DownloadError("ERROR: [generic] HTTP Error 403: Forbidden")

    monkeypatch.setattr(routes_mod, "inspect_url", blocked)
    monkeypatch.setattr(routes_mod.youtube_circuit, "is_open", lambda *a: False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/info", json={"url": "https://vimeo.com/123"})

    # Not a YouTube URL, so no upsell: it stays an ordinary failure.
    assert r.status_code != 451


@pytest.mark.asyncio
async def test_a_403_from_youtube_becomes_the_upsell_not_a_generic_error(monkeypatch):
    from app.api import routes as routes_mod
    import yt_dlp

    from app.services import extractor as extractor_mod

    def blocked(url):
        raise yt_dlp.utils.DownloadError(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )

    # Stub the network layer, not inspect_url: the classifier under test is
    # exactly the code that would otherwise be bypassed.
    monkeypatch.setattr(extractor_mod, "_sync_extract_info", blocked)
    monkeypatch.setattr(routes_mod.youtube_circuit, "is_open", lambda *a: False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/info", json={"url": "https://youtu.be/abc"})

    assert r.status_code == 451
    detail = r.json()["detail"]
    assert detail["code"] == "UPSTREAM_BLOCKED"
    assert detail["remedy"] == "native_app"


def test_job_failures_can_carry_a_code_to_the_client():
    from app.services.jobs import job_manager
    from app.api.schemas import DownloadRequest

    job_manager._jobs.clear()
    job = job_manager.create_job(DownloadRequest(url="https://youtu.be/a", mode="mp4"))
    job_manager.update_job(job.id, status="error", error="blocked", code="UPSTREAM_BLOCKED")
    resp = job_manager.get_job(job.id).to_response()

    assert resp.code == "UPSTREAM_BLOCKED"
    assert resp.error == "blocked"


# --- Settings diagnostics & update check (§1.7) ---------------------------

def test_version_is_reported_in_capabilities():
    from app import config

    transport = ASGITransport(app=app)
    async def run():
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/api/capabilities")

    import asyncio
    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert resp.json()["version"] == config.APP_VERSION


def test_version_parse_orders_correctly():
    from app.api.routes import _parse_version

    assert _parse_version("v0.2.0") > _parse_version("v0.1.9")
    assert _parse_version("1.10.0") > _parse_version("1.9.0")
    assert _parse_version("v0.1.1") == _parse_version("0.1.1")
    # Unreadable input must never look newer than what is running.
    assert _parse_version("not-a-version") == (0, 0, 0)
    assert not (_parse_version("nightly") > _parse_version("0.1.6"))


@pytest.mark.asyncio
async def test_update_check_reports_a_newer_release(monkeypatch):
    import httpx
    from app.api import routes as routes_mod

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"tag_name": "v9.9.9",
                    "html_url": "https://github.com/x/y/releases/tag/v9.9.9"}

    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)
    monkeypatch.setattr(routes_mod.config, "APP_VERSION", "v0.1.0")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/update-check")

    assert r.status_code == 200
    body = r.json()
    assert body["update_available"] is True
    assert body["latest"] == "v9.9.9"
    assert body["current"] == "v0.1.0"


@pytest.mark.asyncio
async def test_update_check_reports_up_to_date(monkeypatch):
    import httpx
    from app.api import routes as routes_mod

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"tag_name": "v0.1.6", "html_url": "https://example.invalid"}

    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)
    monkeypatch.setattr(routes_mod.config, "APP_VERSION", "v0.1.6")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/update-check")

    assert r.json()["update_available"] is False


@pytest.mark.asyncio
async def test_update_check_says_so_when_it_cannot_tell(monkeypatch):
    """A failed check must not be reported as 'no update available'."""
    import httpx
    from app.api import routes as routes_mod

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)
    monkeypatch.setattr(httpx, "get", boom)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/update-check")

    assert r.status_code == 503
    assert "update_available" not in r.json()


@pytest.mark.asyncio
async def test_dev_build_skips_version_comparison(monkeypatch):
    import httpx
    from app.api import routes as routes_mod

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"tag_name": "v9.9.9", "html_url": "https://example.invalid"}

    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)
    monkeypatch.setattr(routes_mod.config, "APP_VERSION", "dev")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/update-check")

    body = r.json()
    assert body["update_available"] is False
    assert "Development build" in body["note"]


@pytest.mark.asyncio
async def test_log_endpoints_return_the_tail(monkeypatch, tmp_path):
    from app.api import routes as routes_mod

    log = tmp_path / "launcher.log"
    log.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    monkeypatch.setattr(routes_mod, "launcher_log_path", lambda: log)
    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/log", params={"tail_bytes": 2048})
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is True
        assert body["truncated"] is True
        assert "line 499" in body["content"]
        assert str(log) == body["path"]


@pytest.mark.asyncio
async def test_log_endpoint_handles_a_missing_file(monkeypatch, tmp_path):
    from app.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "launcher_log_path", lambda: tmp_path / "nope.log")
    monkeypatch.setattr(routes_mod.config, "IS_HOSTED", False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/diagnostics/log")

    body = r.json()
    assert body["exists"] is False
    assert body["content"] == ""
    assert "message" in body


@pytest.mark.asyncio
async def test_diagnostics_are_refused_on_hosted():
    """Hosted has no local log to open and is always current."""
    from app.api import routes as routes_mod

    monkeypatch_holder = routes_mod.config
    original = monkeypatch_holder.IS_HOSTED
    monkeypatch_holder.IS_HOSTED = True
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/api/diagnostics/log")).status_code == 404
            assert (await client.post("/api/diagnostics/open-log")).status_code == 404
            assert (await client.get("/api/diagnostics/update-check")).status_code == 404
    finally:
        monkeypatch_holder.IS_HOSTED = original


def test_log_path_is_defined_once_and_shared():
    """Two definitions would drift, and the panel would open the wrong file."""
    from app.services.logpaths import launcher_log_path

    root = Path(__file__).resolve().parent.parent
    assert launcher_log_path().name == "launcher.log"
    assert "launcher_log_path" in (root / "run_desktop.py").read_text()
    assert "app.services.logpaths" in (root / "app" / "api" / "routes.py").read_text()
