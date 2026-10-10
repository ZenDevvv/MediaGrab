import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import STATIC_DIR
from app.api.routes import router as api_router
from app.services.cleaner import periodic_cleaner_daemon

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

# Root SPA endpoints
@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

@app.get("/choose", include_in_schema=False)
@app.get("/choose.html", include_in_schema=False)
async def serve_choose():
    return FileResponse(STATIC_DIR / "choose.html", media_type="text/html")
