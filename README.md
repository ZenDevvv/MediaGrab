# MediaGrab

A lightweight, self-hosted media utility designed to inspect, clip, convert, and download media from over 1,750+ online video and audio platforms (including YouTube, Vimeo, TikTok, SoundCloud, Reddit, X/Twitter) into MP4 videos, MP3 audio tracks, or SRT subtitles.

Built as an installable Progressive Web App (PWA) with zero database requirements, an in-memory job pipeline, direct stream remuxing, and automatic 30-minute file pruning.

---

## Features

- **PWA-First Architecture:** Installable on desktop and mobile (`manifest.webmanifest`), offline shell caching via Service Worker (`sw.js`), and responsive single/two-column layout based on [`DESIGN.md`](./DESIGN.md).
- **Remux-First Video (MP4):** Direct stream copying (`-c copy`) for AVC1/H.264 and AAC streams, eliminating CPU-intensive transcoding and saving battery.
- **Precision Time Trimmer:** Uses `yt-dlp` keyframe stream slicing (`download_ranges`) to fetch only the requested clip instead of downloading multi-gigabyte source streams.
- **Audio Conversion (MP3):** Extracts audio with selectable bitrates (320, 192, 128 kbps), automatically tags ID3 metadata (Title, Artist, Year), and embeds video thumbnails as cover art.
- **Instant Transcripts (SRT):** Bypasses audio/video stream downloads completely to fetch captions and convert them to SubRip `.srt` in seconds.
- **Real-Time SSE Telemetry:** Server-Sent Events stream live download throughput, percentage, ETA, and pipeline stage progress.
- **Ephemeral Storage:** Server deletes completed files 5 minutes after download retrieval, and an automated background sweeper purges any files older than 30 minutes.

---

## Quickstart

### Prerequisites

- Python 3.10+ (tested on Python 3.13 & 3.14)
- `ffmpeg` installed and available on PATH
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

### 1. Run with `uv`

```bash
# Clone or navigate to the repository
cd /path/to/MediaGrab

# Create virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt

# Start the server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Visit [http://localhost:8000](http://localhost:8000) in your browser.

### 2. Run with Docker

```bash
docker build -t mediagrab .
docker run -p 8000:8000 mediagrab
```

---

## Testing

Run the automated test suite with pytest:

```bash
uv run pytest tests/ -v
```

---

## Project Structure

```text
MediaGrab/
├── app/
│   ├── main.py              # FastAPI app factory, lifespan, static mounting
│   ├── config.py            # App settings & ephemeral file lifecycle
│   ├── api/
│   │   ├── routes.py        # /api/info, /api/download, /api/events, /api/file
│   │   └── schemas.py       # Pydantic v2 data models
│   └── services/
│       ├── extractor.py     # yt-dlp metadata probing and stream analysis
│       ├── downloader.py    # yt-dlp + FFmpeg orchestration & progress hooks
│       ├── jobs.py          # Thread-safe in-memory JobManager & SSE queues
│       └── cleaner.py       # Background cleanup daemon & deferred file removal
├── static/
│   ├── index.html           # Production PWA single-page application
│   ├── manifest.webmanifest # PWA Web App Manifest
│   ├── sw.js                # App shell caching Service Worker
│   ├── css/
│   │   └── app.css          # Design tokens & responsive styles (DESIGN.md)
│   ├── js/
│   │   └── app.js           # Native JS controller (trim engine, SSE, storage)
│   └── icons/               # SVG & PNG app icons (192, 512, maskable)
├── tests/                   # Automated pytest suite
├── downloads/               # Ephemeral storage directory
├── DESIGN.md                # Design system specification
├── ARCHITECTURE.md          # Architectural specification
└── requirements.txt         # Minimal Python dependencies
```

---

## License

MIT
