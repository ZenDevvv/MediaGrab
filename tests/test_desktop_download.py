import pytest


def test_desktop_download_from_a_local_server(tmp_path, monkeypatch):
    """Network-free regression test for the desktop output-location bug."""
    import functools, http.server, socketserver, threading, asyncio
    from pathlib import Path
    from app.api.schemas import DownloadRequest
    from app.services.downloader import execute_download
    from app.services.jobs import job_manager

    fixture = Path(__file__).parent / "fixtures" / "sample.mp4"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(fixture.parent))
    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True
    httpd = Quiet(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    out = tmp_path / "downloads"
    monkeypatch.setenv("MEDIAGRAB_MODE", "desktop")
    monkeypatch.setenv("USER_FACING_FILENAMES", "true")
    monkeypatch.setenv("RETENTION_ENABLED", "false")
    monkeypatch.setattr("app.services.downloader.config.DOWNLOADS_DIR", out)
    monkeypatch.setattr("app.services.downloader.config.USER_FACING_FILENAMES", True)

    url = f"http://127.0.0.1:{httpd.server_address[1]}/sample.mp4"
    try:
        req = DownloadRequest(url=url, quality="720p", mode="mp4")
        job = job_manager.create_job(req)
        asyncio.run(execute_download(job.id, req))
        j = job_manager.get_job(job.id)
        assert j.status == "ready", f"{j.status}: {j.error}"
        # The output template must keep the separator before the extension.
        assert j.filename and j.filename.endswith(".mp4"), j.filename
        assert not any(p.name.startswith(".mediagrab-") for p in out.iterdir()), \
            "per-job staging directory was left behind"
    finally:
        httpd.shutdown()
