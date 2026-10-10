# MediaGrab — Architecture Specification (ARCHITECTURE.md)

**Version:** 1.0.0  
**Status:** Approved Architecture Specification  
**Target Platform:** Installable Progressive Web App (PWA) + Lightweight Self-Hosted Python Backend  
**Design Reference:** [`DESIGN.md`](file:///home/machenike/Projects/MediaGrab/DESIGN.md) & [`MediaGrab.html`](file:///home/machenike/Projects/MediaGrab/MediaGrab.html)

---

## 1. Architectural Principles & System Goals

MediaGrab is a self-hosted, lightweight media utility designed to inspect, clip, convert, and download media from 1,750+ online video and audio platforms. It is engineered around three core principles:

1. **Lightweight & Dependency-Minimal ("Zero-Slop"):**
   - **No Database:** No SQLite, PostgreSQL, or Redis. Jobs exist purely in-memory with TTL cleanup; download history is strictly local in client `localStorage`.
   - **No Heavy Frontend Bundler:** No Node.js build pipelines, Webpack, or Vite required in production. Built with vanilla ES6+, native Web APIs (`EventSource`, Service Worker, `localStorage`), and native CSS custom properties directly matching [`DESIGN.md`](file:///home/machenike/Projects/MediaGrab/DESIGN.md).
2. **Remux-First & Hardware-Friendly Execution:**
   - Prioritizes direct container stream copying (`-c copy`) for MP4 downloads. Minimizes multi-core CPU transcoding and inrush wattage spikes, preserving battery health and thermal quietness on mobile and laptop hosts.
   - Precision keyframe trimming (`download_ranges` with `force_keyframes_at_cuts`) downloads only requested temporal slices instead of multi-gigabyte source streams.
3. **PWA-First Offline Shell:**
   - Fully installable on desktop and mobile (`manifest.webmanifest`, standalone mode, maskable icons).
   - Dedicated Service Worker (`sw.js`) caches the application shell, fonts, and static assets for sub-50ms loads and graceful offline state handling, while strictly bypassing media payload caching.

---

## 2. High-Level System Architecture

```mermaid
flowchart TD
    subgraph Client ["Client Tier (PWA / Browser SPA)"]
        UI["UI Layer\n(HTML5 + CSS Tokens)"]
        SW["Service Worker (sw.js)\n(Shell Caching & Offline Screen)"]
        JS["Native JS Controller (app.js)\n- State Machine & Router\n- Range Timeline & Trim Engine\n- LocalStorage History Store"]
        SSE["SSE / Polling Consumer\n(EventSource /api/events/{id})"]
    end

    subgraph Server ["Server Tier (FastAPI Service)"]
        Router["FastAPI Application (main.py / routes.py)\n- POST /api/info\n- POST /api/download\n- GET /api/events/{id}\n- GET /api/file/{id}"]
        JobMgr["In-Memory Job Manager (jobs.py)\nDict[UUID, JobState] + Event Broadcasters"]
        Cleaner["Ephemeral Storage Reaper (cleaner.py)\n- Deferred 5-min post-serve delete\n- 5-min periodic orphan purge (>30m)"]
    end

    subgraph Workers ["Media Engines"]
        YTDL["yt-dlp Engine (extractor.py)\n- Fast Info Extraction (skip_download)\n- Chunk & Keyframe Stream Clipper"]
        FFMPEG["FFmpeg Stream Engine\n- Remuxing (AVC1 + AAC -> MP4)\n- Audio Conversion (MP3 + ID3 + Artwork)\n- Subtitle Extraction / mov_text Embedding"]
    end

    subgraph Storage ["Local Ephemeral Storage"]
        Disk[("downloads/\nTemporary .mp4 / .mp3 / .srt")]
    end

    UI --> JS
    JS --> SW
    JS -- "HTTP POST (Inspect/Start)" --> Router
    SSE -- "Server-Sent Events" <-- Router
    Router --> JobMgr
    Router --> Cleaner
    JobMgr --> YTDL
    JobMgr --> FFMPEG
    YTDL --> Disk
    FFMPEG --> Disk
    Router -- "Binary Stream & Delete" --> Disk
    Disk --> Cleaner
```

---

## 3. Repository Directory Structure

A minimalist layout keeping backend code isolated, modular, and frontend assets statically servable directly by FastAPI:

```text
MediaGrab/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app factory, lifespan, static mounting
│   ├── config.py            # App settings (storage path, TTLs, max concurrency)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py        # /api/info, /api/download, /api/events, /api/file
│   │   └── schemas.py       # Pydantic v2 models (InfoResponse, DownloadRequest)
│   └── services/
│       ├── __init__.py
│       ├── extractor.py     # yt-dlp metadata probing and stream analysis
│       ├── downloader.py    # yt-dlp + FFmpeg orchestration & progress hooks
│       ├── jobs.py          # Thread-safe in-memory JobManager & SSE queues
│       └── cleaner.py       # Background cleanup daemon & deferred file removal
├── static/
│   ├── index.html           # Main PWA SPA view (derived from MediaGrab.html)
│   ├── manifest.webmanifest # PWA Web App Manifest
│   ├── sw.js                # App shell caching Service Worker
│   ├── css/
│   │   └── app.css          # Design tokens & responsive styles from DESIGN.md
│   ├── js/
│   │   └── app.js           # Native JS controller (SPA state, trim, SSE, storage)
│   └── icons/
│       ├── icon-192.png     # PWA standard icon
│       ├── icon-512.png     # PWA high-res icon
│       ├── icon-maskable-512.png # PWA maskable adaptive icon
│       ├── favicon.svg      # SVG vector favicon
│       ├── favicon.ico      # Multi-resolution ICO favicon
│       ├── apple-touch-icon-180.png # iOS home screen icon
│       └── mediagrab-*.svg  # Brand marks, lockups, and wordmark
├── downloads/               # Ephemeral storage directory (ignored in git)
├── DESIGN.md                # Design system specification
├── MediaGrab.html           # Original prototype & design mockup
├── ARCHITECTURE.md          # This architectural blueprint
├── requirements.txt         # Minimal Python dependencies
├── Dockerfile               # Production container definition (Python + FFmpeg)
└── README.md
```

---

## 4. Client Tier (Progressive Web App)

### 4.1 Single-Page State Machine
The client interface implements a deterministic, hash-optional view state model:

| State | Role | Key Interactions |
|---|---|---|
| `PASTE` | Landing & URL ingestion | Clipboard paste, URL validation, "Your files" drawer toggle |
| `CHOOSE_VIDEO` | MP4 format configuration | Quality chips (2160p-360p), remux vs encode badge, Trim & Caption rows |
| `CHOOSE_AUDIO` | MP3 format configuration | Source bitrate scale indicator (e.g. 129 kbps Opus), 320/192/128 kbps tiers |
| `TRIM_STEP` | Keyframe range selector | Dual range slider + mm:ss inputs + real-time savings & file size calculation |
| `SUBS_STEP` | Subtitles & Transcript | Language selection, instant `.srt` download, soft MP4 embed toggle |
| `WORKING` | Real-time progress display | SSE telemetry (percentage, speed, ETA, downloaded MB), active step highlights |
| `READY` | Artifact delivery | One-tap save for media & subtitles, 30-minute self-deletion countdown |
| `ERROR` | Sanitized error report | User-friendly explanation (unsupported URL, private video, network failure) |
| `OFFLINE` | PWA offline screen | Network disconnect notification; displays already cached/saved file list |

### 4.2 Progressive Web App Configuration
- **Manifest (`manifest.webmanifest`):**
  - `name`: `"MediaGrab"`
  - `short_name`: `"MediaGrab"`
  - `start_url`: `"/"`
  - `display`: `"standalone"`
  - `background_color`: `"#101722"` (dark) / `"#F5F7F8"` (light)
  - `theme_color`: Follows color scheme via dynamic JS update and dual `<meta name="theme-color">` elements.
  - Icons: 192x192, 512x512, and 512x512 maskable with the official two-bracket play mark.
- **Service Worker Lifecycle (`sw.js`):**
  - **Cache Name:** `mediagrab-shell-v4`
  - **Pre-cached Assets:** `/`, `/css/app.css`, `/js/app.js`, `/manifest.webmanifest`, SVG symbols, and Google Fonts (`DM Mono`, `Gabarito`, `Onest`).
  - **Network-Only Bypass:** `/api/*` and any media stream routes (`/api/file/*`) explicitly bypass service worker cache to prevent quota exhaustion and memory stalls.
  - **Offline Fallback:** If network fetch fails on navigation or API requests, the controller transitions into the styled `OFFLINE` view.
  - **Install Prompt:** Captures `beforeinstallprompt`, reveals the install strip/card, and dismisses on user action.

### 4.3 Native Client Controller Modules (`app.js`)
- **`ThemeController`:** Toggles `system` / `light` / `dark`, sets `data-theme` on `<html>`, persists preference in `localStorage.getItem('mg-theme')`.
- **`HistoryStore`:** Persists completed downloads (`job_id`, title, format, file_size, timestamp) in browser `localStorage`. Powers the desktop and mobile "Your files" panel with zero server-side database.
- **`TrimEngine`:** Synchronizes dual range sliders (`#trim-s`, `#trim-e`), waveform visualization, typed `mm:ss` inputs, and dynamically recomputes estimated file size across all quality chips.
- **`EventClient`:** Initializes `new EventSource('/api/events/' + jobId)` with automatic polling fallback to `/api/progress/{jobId}` if SSE connection drops or is blocked by reverse proxies.

---

## 5. Backend Tier (FastAPI Engine)

### 5.1 Technology Selection
- **FastAPI (ASGI):** Native async request handling, minimal memory overhead (<30MB baseline RAM), built-in background tasks, and native SSE streaming support.
- **yt-dlp:** Direct Python module invocation (`yt_dlp.YoutubeDL`), avoiding shell injection risks and maximizing performance via structured parameter dictionaries.
- **FFmpeg:** Invoked as a managed subprocess or via yt-dlp post-processors for keyframe-accurate cuts, remuxing, MP3 tagging, and subtitle injection.

### 5.2 Core Backend Modules

#### 1. In-Memory Job Manager (`app/services/jobs.py`)
Maintains an active registry of jobs:
```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"

class Job:
    id: str                      # 8-character compact UUID
    url: str
    status: JobStatus
    progress: float              # 0.0 to 100.0
    speed: str                   # e.g. "4.2 MB/s"
    eta: str                     # e.g. "0:03"
    downloaded_bytes: int
    total_bytes: int
    stage: str                   # e.g. "Joining video and audio into an MP4"
    file_path: Optional[str]
    created_at: float
    expires_at: float            # created_at + 1800s (30 mins)
    error_message: Optional[str]
    subscribers: List[asyncio.Queue]  # SSE broadcast queues
```

#### 2. Media Extractor (`app/services/extractor.py`)
Inspects metadata without downloading stream payload:
- Uses `YoutubeDL({'skip_download': True, 'extract_flat': False})`.
- Aggregates available video streams to build a normalized quality matrix: `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`.
- Tags each resolution as `remux` (if compatible H.264/AAC or native MP4) or `encode` (if VP9/AV1 requires transcoding or higher CPU load).
- Calculates audio source bitrate (e.g., Opus ~129 kbps) and classifies subtitles into creator-uploaded vs auto-generated.

#### 3. Media Downloader & Processor (`app/services/downloader.py`)
Executes the requested payload in a worker thread (`asyncio.to_thread`):
- **Remux-First Video:**
  - Formats: `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best`
  - Subtitle embed: `FFmpegEmbedSubtitle` (`mov_text` track)
  - Time Trim: `download_ranges` with keyframe-accurate cut boundaries.
- **Audio Conversion (MP3):**
  - FFmpeg post-processor: extracts best audio, converts to MP3 at target bitrate (`128k`, `192k`, `320k`).
  - Metadata Injector: `FFmpegMetadata` tags track title, artist/channel, upload date, and attaches thumbnail as front cover art (`ID3v2`).
- **Instant Transcript (SRT):**
  - Extracts requested subtitle track with `skip_download=True`, converts to SubRip `.srt`, finishes in < 2 seconds.
- **Progress Hook:**
  - Pushes real-time progress events into the job's `asyncio.Queue` subscribers to immediately push SSE chunks to connected clients.

#### 4. Ephemeral Storage Reaper (`app/services/cleaner.py`)
Enforces the 30-minute self-deletion guarantee:
- **Deferred Post-Download Cleanup:** When `GET /api/file/{job_id}` completes sending the file response, a background task schedules file deletion after a 5-minute grace period.
- **Periodic Orphan Sweeper:** An async loop runs every 5 minutes in the FastAPI lifespan context, checking `downloads/` and removing any file whose age exceeds 30 minutes.

---

## 6. API Interface Specification

### `POST /api/info`
Inspects a media URL and returns platform metadata.
- **Request:**
  ```json
  { "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ" }
  ```
- **Response (200 OK):**
  ```json
  {
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "How to keep a sourdough starter alive",
    "uploader": "Crumb & Co. Kitchen",
    "duration": 252,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "source_audio_kbps": 129,
    "source_audio_codec": "opus",
    "qualities": [
      { "resolution": "2160p", "approx_bytes": 429916160, "type": "encode" },
      { "resolution": "1080p", "approx_bytes": 100663296, "type": "remux" },
      { "resolution": "720p", "approx_bytes": 50331648, "type": "remux" }
    ],
    "subtitles": [
      { "lang": "en", "label": "English", "is_auto": false },
      { "lang": "es", "label": "Spanish", "is_auto": false }
    ]
  }
  ```

---

### `POST /api/download`
Initiates a background download and returns an asynchronous job ID.
- **Request:**
  ```json
  {
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "mode": "mp4",               // "mp4" | "mp3" | "srt"
    "quality": "1080p",          // for mp4
    "bitrate": 192,              // for mp3 (128 | 192 | 320)
    "trim": {
      "start": 15,
      "end": 90
    },
    "subtitles": {
      "lang": "en",
      "embed": true
    }
  }
  ```
- **Response (202 Accepted):**
  ```json
  {
    "job_id": "a9b8c7d6"
  }
  ```

---

### `GET /api/events/{job_id}`
Real-time Server-Sent Events (SSE) telemetry stream (`text/event-stream`).
- **SSE Data Stream Format:**
  ```text
  event: progress
  data: {"progress": 62.5, "speed": "4.2 MB/s", "eta": "0:03", "stage": "Downloading 00:15 to 01:30", "got": "18 / 29 MB"}

  event: complete
  data: {"filename": "how-to-keep-a-sourdough-starter-alive.mp4", "size": "29.1 MB", "download_url": "/api/file/a9b8c7d6"}

  event: error
  data: {"error": "Platform rate limit or stream unavailable"}
  ```

---

### `GET /api/progress/{job_id}`
Polling fallback returning the current snapshot of `JobState`.

---

### `GET /api/file/{job_id}`
Downloads the completed binary payload (`application/octet-stream` or `video/mp4`).
- Injects `Content-Disposition: attachment; filename="<sanitized_filename>"`
- Registers FastAPI `BackgroundTask` for grace-period deferred file removal.

---

## 7. Performance & Hardware Protection Strategy

To ensure optimal efficiency on personal workstations and battery-powered laptops (e.g. Intel Core i7-11800H / RTX 3050 Ti):

1. **Remux-First Execution:** Direct stream copy avoids pegging all CPU cores at PL2 boost frequencies, preventing voltage sag on battery power and keeping fan acoustics silent.
2. **Keyframe-Accurate Stream Slicing:** By using yt-dlp native range requests (`download_ranges`), trimming a 1-minute clip from a 2-hour lecture downloads only ~30MB instead of downloading 4GB and trimming locally.
3. **No Heavy Database Locks:** Zero persistent disk I/O for job telemetry; in-memory data structures produce zero SSD write amplification.
4. **Immediate Temp Purge:** Failed or aborted downloads automatically purge orphaned `.part` and temporary audio files.

---

## 8. Development & Implementation Roadmap

| Phase | Milestone | Deliverables |
|---|---|---|
| **Phase 1** | App Shell & PWA Setup | Migrate [`MediaGrab.html`](file:///home/machenike/Projects/MediaGrab/MediaGrab.html) into clean `static/index.html`, `app.css`, `app.js`, `manifest.webmanifest`, and `sw.js`. Verify offline handling and responsive layout. |
| **Phase 2** | Backend Core & Extractor | Implement `app/main.py`, `app/services/extractor.py`, and `POST /api/info` with yt-dlp parameter normalization. |
| **Phase 3** | Download & FFmpeg Engine | Build `app/services/downloader.py` supporting Remux MP4, MP3 conversion with ID3 tags, and `.srt` subtitle extraction. |
| **Phase 4** | Real-Time SSE Pipeline | Implement `app/services/jobs.py` and `GET /api/events/{job_id}` SSE streaming + polling fallback. |
| **Phase 5** | Lifecycle Cleaner & Hardening | Implement `app/services/cleaner.py` (30-min reaper and 5-min post-serve purge), verify containerization and unit tests. |

---

*This architecture document serves as the single source of truth for MediaGrab's lightweight implementation.*
