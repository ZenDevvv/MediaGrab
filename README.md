# MediaGrab

A lightweight, self-hosted media utility designed to inspect, clip, convert, and download media from over 1,750+ online video and audio platforms (including YouTube, Vimeo, TikTok, SoundCloud, Reddit, X/Twitter) into MP4 videos, MP3 audio tracks, or SRT subtitles.

Runs three ways from one codebase: a **hosted web instance** with guardrails and zero retention, a **local desktop application**, and the **PWA** in a browser.

---

## Features

- **Three deployment modes:** hosted cloud instance, native desktop app (Windows/Linux), or browser PWA — all sharing one `/api/` contract.
- **Dual-Mode Guardrails:** In `hosted` mode the server caps resolution (1080p), duration (60 min, clip-aware), and file size (1 GB), enforced against *server-side* metadata so the caps cannot be spoofed.
- **Remux-First Video (MP4):** Direct stream copying (`-c copy`) for AVC1/H.264 and AAC streams, eliminating CPU-intensive transcoding and saving battery.
- **Precision Time Trimmer:** Uses `yt-dlp` keyframe stream slicing (`download_ranges`) to fetch only the requested clip instead of downloading multi-gigabyte source streams.
- **Audio Conversion (MP3):** Extracts audio with selectable bitrates (320, 192, 128 kbps), automatically tags ID3 metadata (Title, Artist, Year), and embeds video thumbnails as cover art.
- **Instant Transcripts (SRT):** Bypasses audio/video stream downloads completely to fetch captions and convert them to SubRip `.srt` in seconds.
- **Real-Time SSE Telemetry:** Server-Sent Events stream live download throughput, percentage, ETA, and pipeline stage progress.
- **Zero-Retention Hosting:** Completed files are deleted 60 seconds after retrieval; a background sweeper removes anything unclaimed.
- **Session-First Rate Limiting:** A signed session cookie disambiguates concurrent-download fairness, so users sharing a carrier NAT do not block each other, with an independent per-IP ceiling as an anti-evasion backstop.

---

## Desktop App

> **Status:** The Linux build is verified working. Windows is built by CI but **not yet manually verified** — no claims are made about it until someone runs it.

### Build from source

```bash
# Bundle a static FFmpeg (required for merge, MP3, subtitles, trimming)
python packaging/fetch_ffmpeg.py

# Produce dist/MediaGrab/
python -m PyInstaller packaging/MediaGrab.spec --noconfirm --distpath dist
```

### Run it

```bash
./dist/MediaGrab/MediaGrab                  # opens a window
./dist/MediaGrab/MediaGrab --no-browser     # headless, useful for smoke tests
./dist/MediaGrab/MediaGrab --no-native      # skip pywebview, use Chromium --app
```

Downloads are written **directly to your Downloads folder** with a clean filename and automatic de-duplication (`Title.mp4`, `Title (1).mp4`). Nothing is deleted on desktop.

### Window fallback

The launcher degrades rather than failing: `pywebview` (native) → Chromium/Chrome `--app` → default browser. If PyGObject is unavailable or ABI-mismatched against your Python, the app still runs.

> **Note on the native window:** `pywebview` requires PyGObject (`gi`) matching your Python version. On many Linux systems `gi` is built for the system Python, which may differ from a virtualenv's. If the native window fails to open, use `--no-native` — functionality is identical.

### Current limitations

- **No installer yet.** There is no AppImage, `.desktop` entry, or `install.sh`; you extract and run. True one-click Linux installation is future work.
- **No published releases yet**, so download links from a hosted instance do not currently resolve.
- **Licensing:** the bundled FFmpeg is a **GPL-3.0** build (BtbN). Redistributing these binaries carries GPL-3.0 obligations. Do not publish binaries until this is resolved.

### CI

`.github/workflows/build-desktop.yml` builds Windows (`.exe`) and Linux (`.tar.gz`) on `v*` tags, smoke-tests the backend on both runners, and attaches artifacts to the GitHub release. Trigger manually via **workflow_dispatch**.

---

## Web App

### Prerequisites

- Python 3.10+ (tested on Python 3.13 & 3.14)
- `ffmpeg` installed and available on PATH
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

### 1. Run with `uv` (desktop mode, unlimited, files retained)

```bash
uv venv
uv pip install -r requirements.txt
uv run uvicorn app.main:app --port 8000 --reload
```

Visit [http://localhost:8000](http://localhost:8000).

### 2. Run with the desktop launcher (window + `~/Downloads` output)

```bash
uv pip install pywebview        # optional; app degrades without it
python run_desktop.py
```

### 3. Run the hosted configuration

```bash
MEDIAGRAB_MODE=hosted BEHIND_PROXY=true uv run uvicorn app.main:app --port 8000
```

Enable guardrails, zero retention, and the native-app promo strip.

### Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `MEDIAGRAB_MODE` | `desktop` | `desktop` (unlimited) or `hosted` (guarded) |
| `BEHIND_PROXY` | `false` | Trust `CF-Connecting-IP` / `X-Forwarded-For`. **Only enable behind a proxy you control** — otherwise clients can forge their IP and bypass the rate limit |
| `RETENTION_ENABLED` | hosted only | File deletion. Keep `false` on desktop so user files are never swept |
| `HOSTED_CONCURRENCY` | `3` | Global concurrent download slots |
| `HOSTED_PER_SESSION_CONCURRENCY` | `1` | Per-session gate |
| `HOSTED_PER_IP_CONCURRENCY` | `3` | Per-IP ceiling (anti-evasion) |
| `HOSTED_MAX_DURATION_SECONDS` | `3600` | Duration cap; SRT exempt |
| `HOSTED_MAX_FILE_SIZE_BYTES` | `1073741824` | Size cap |
| `SESSION_SECRET` | *(generated)* | **Set this in production.** Unset means a per-process key, so sessions reset on every restart |
| `MEDIA_GRAB_DOWNLOADS_DIR` | `./downloads` | Desktop output directory |

### 4. Docker (hosted instance)

```bash
docker build -t mediagrab .
docker run -p 8000:8000 mediagrab
```

The image is configured for `hosted` mode. It is a server deployment, not a desktop app — a container cannot write to your host's Downloads folder.

---

## Testing

```bash
uv run pytest tests/ -v
```

The suite runs in both modes and covers the guardrails, scheduler, retention, and a real headless-Chromium journey of the UI:

```bash
MEDIAGRAB_MODE=hosted uv run pytest tests/ -v
```

Browser tests are skipped automatically if `chromium` or `node` is unavailable.

---

## Project Structure

```text
MediaGrab/
├── app/
│   ├── main.py              # FastAPI app factory, lifespan, mode injection
│   ├── config.py            # Dual-mode settings & ephemeral file lifecycle
│   ├── api/
│   │   ├── routes.py        # /api/info, /api/download, /api/capabilities, /api/abort
│   │   └── schemas.py       # Pydantic v2 data models
│   └── services/
│       ├── extractor.py     # yt-dlp metadata probing and stream analysis
│       ├── downloader.py    # yt-dlp + FFmpeg orchestration, cancellation, naming
│       ├── jobs.py          # Thread-safe in-memory JobManager & SSE queues
│       ├── scheduler.py     # 3-slot FIFO queue with session + IP gates
│       ├── metadata.py      # Cached inspection and hosted guardrails
│       ├── network.py       # Client IP + signed session identity
│       ├── cleaner.py       # Retention daemon & deferred file removal
│       └── templates.py     # Server-side mg-mode injection
├── static/                  # Frontend (PWA shell + hosted gating)
├── tests/                   # pytest suite, incl. headless-Chromium E2E
├── run_desktop.py           # Desktop launcher with window fallbacks
├── packaging/
│   ├── MediaGrab.spec       # PyInstaller build definition
│   └── fetch_ffmpeg.py      # Static FFmpeg bundler
├── .github/workflows/
│   └── build-desktop.yml    # Windows + Linux builds on tags
├── MULTI_PLATFORM_IMPLEMENTATION_PLAN.md  # Architecture, decisions, milestones
├── DESIGN.md                # Design system specification
└── ARCHITECTURE.md          # Architectural specification
```

---

## License

MIT

> Bundled FFmpeg binaries are **GPL-3.0** (BtbN builds) and are covered by that licence, not MIT. `yt-dlp` is Unlicense; `youtubedl-android` (Android client) is GPL-3.0.
