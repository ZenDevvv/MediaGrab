import pytest
from httpx import AsyncClient, ASGITransport
from app import config
from app.main import app
from app.services.jobs import job_manager


# These use placeholder URLs that only resolve to a job in desktop mode. Hosted
# mode now rejects them at the metadata guardrail, which is the correct outcome.
requires_desktop = pytest.mark.skipif(
    config.IS_HOSTED,
    reason="placeholder URLs are rejected by hosted guardrails before a job is created",
)

@pytest.mark.asyncio
async def test_pwa_root_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Index HTML
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "<title>MediaGrab</title>" in resp.text

        # Choose HTML
        resp = await client.get("/choose")
        assert resp.status_code == 200
        assert "Configure Media" in resp.text
        assert 'id="view-choose"' in resp.text

        resp = await client.get("/choose.html")
        assert resp.status_code == 200
        assert "Configure Media" in resp.text

        # Manifest
        resp = await client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert "MediaGrab" in resp.text

        # Service Worker
        resp = await client.get("/sw.js")
        assert resp.status_code == 200
        assert "CACHE_NAME" in resp.text

        # Favicons
        resp = await client.get("/favicon.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")

        resp = await client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/x-icon")

@pytest.mark.asyncio
@requires_desktop
async def test_api_download_and_progress_flow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid info request
        resp = await client.post("/api/info", json={"url": "not-a-valid-url-domain"})
        assert resp.status_code == 400

        # Create download job
        download_payload = {
            "url": "https://www.youtube.com/watch?v=mocktest",
            "mode": "mp4",
            "quality": "1080p"
        }
        resp = await client.post("/api/download", json=download_payload)
        assert resp.status_code == 202
        data = resp.json()
        job_id = data["job_id"]
        assert job_id is not None

        # Check progress snapshot
        resp = await client.get(f"/api/progress/{job_id}")
        assert resp.status_code == 200
        progress_data = resp.json()
        assert progress_data["job_id"] == job_id
        assert progress_data["status"] in ["queued", "extracting", "downloading", "error"]

@pytest.mark.asyncio
@requires_desktop
async def test_api_events_stream():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        req_payload = {
            "url": "https://www.youtube.com/watch?v=streamtest",
            "mode": "mp4"
        }
        resp = await client.post("/api/download", json=req_payload)
        job_id = resp.json()["job_id"]

        # Connect to SSE endpoint
        async with client.stream("GET", f"/api/events/{job_id}") as stream_resp:
            assert stream_resp.status_code == 200
            assert "text/event-stream" in stream_resp.headers["content-type"]

            # Read first chunk
            async for chunk in stream_resp.aiter_text():
                assert "event: progress" in chunk
                break

@pytest.mark.asyncio
async def test_static_assets_integrity():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check app.js is served and doesn't contain undeclared references
        resp_js = await client.get("/js/app.js")
        assert resp_js.status_code == 200
        assert "extractUrlTimestamp(targetUrl)" in resp_js.text or "extractUrlTimestamp" in resp_js.text
        assert "extractUrlTimestamp(url)" not in resp_js.text

        # Check app.css err-line has positive margin to prevent overlapping pill border
        resp_css = await client.get("/css/app.css")
        assert resp_css.status_code == 200
        assert "margin-top: 10px;" in resp_css.text
