import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from app import config
from app.config import STATIC_DIR
from app.api.routes import router as api_router
from app.services.cleaner import periodic_cleaner_daemon
from app.services.templates import render_html

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Launch background cleaner daemon
    cleaner_task = asyncio.create_task(periodic_cleaner_daemon())
    try:
        yield
    finally:
        # Shutdown: Cancel cleaner task
        cleaner_task.cancel()
        try:
            await cleaner_task
        except asyncio.CancelledError:
            pass

app = FastAPI(
    title="MediaGrab",
    version="1.0.0",
    description="Lightweight self-hosted media downloader, trimmer, and converter (PWA)",
    lifespan=lifespan
)

# CORS middleware for open accessibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(api_router)

# Mount static asset subdirectories
app.mount("/css", StaticFiles(directory=STATIC_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=STATIC_DIR / "js"), name="js")
app.mount("/icons", StaticFiles(directory=STATIC_DIR / "icons"), name="icons")

# PWA Root Assets
@app.get("/manifest.webmanifest", include_in_schema=False)
async def serve_manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json"
    )

@app.get("/sw.js", include_in_schema=False)
async def serve_sw():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"}
    )

@app.get("/favicon.svg", include_in_schema=False)
async def serve_favicon():
    return FileResponse(
        STATIC_DIR / "icons" / "favicon.svg",
        media_type="image/svg+xml"
    )

@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon_ico():
    return FileResponse(
        STATIC_DIR / "icons" / "favicon.ico",
        media_type="image/x-icon"
    )

# Root SPA endpoints
def _serve_page(filename: str) -> HTMLResponse:
    """Serve an HTML page with the active execution mode injected.

    Mode is authoritative from the server tag; the frontend only uses
    /api/capabilities for numeric limits and download URLs (§1.2 Decision #9).
    """
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(
        content=render_html(
            html,
            mode="hosted" if config.IS_HOSTED else "desktop",
            retention_seconds=config.CLEANUP_GRACE_SECONDS,
        ),
        media_type="text/html",
    )

@app.get("/", include_in_schema=False)
async def serve_index():
    return _serve_page("index.html")

@app.get("/downloads", include_in_schema=False)
async def serve_downloads():
    return FileResponse(STATIC_DIR / "downloads.html", media_type="text/html")


@app.get("/choose", include_in_schema=False)
@app.get("/choose.html", include_in_schema=False)
async def serve_choose():
    return _serve_page("choose.html")
