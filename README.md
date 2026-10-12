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

**Windows:** download `MediaGrab-Windows.zip`, extract it, double-click `MediaGrab.exe`.

**Linux:** download `MediaGrab-Linux-x86_64.tar.gz`, extract it, run `./MediaGrab/MediaGrab`.

No configuration, environment variables, or setup of any kind is required — the launcher sets its own safe defaults, finds your Downloads folder automatically, and bundles FFmpeg.

```bash
# Optional flags
./MediaGrab --no-browser     # backend only, no window (smoke tests)
./MediaGrab --no-native      # skip the native window, use a Chromium/Edge window
```

Downloads are written **directly to your Downloads folder** with a clean filename and automatic de-duplication (`Title.mp4`, `Title (1).mp4`). Nothing is deleted on desktop.

> The release downloads are a `.zip` / `.tar.gz` because PyInstaller produces a
> folder-based bundle. The `.exe` on its own will not run — it requires the
> `_internal` directory beside it.

### Window fallback

The launcher degrades rather than failing: `pywebview` (native, uses the WebView2 runtime built into Windows) → Chromium/Edge app window → default browser. If none of these are available the app still runs, opening in your browser.

> **Note on the native window:** `pywebview` requires PyGObject (`gi`) matching your Python version. On many Linux systems `gi` is built for the system Python, which may differ from a virtualenv's. If the native window fails to open, use `--no-native` — functionality is identical.

### Current limitations

- **No installer yet.** You download, extract, and run. There is no `Setup.exe`, AppImage, or Start Menu entry. That is acceptable for extraction but not yet a true one-click install.
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

### Public exposure

Freestyle VMs have no public IP, so the app needs an ingress path to be
reachable. Two options:

**A `*.style.dev` subdomain (recommended).** Free, permanent, with a
platform-managed wildcard certificate — no DNS records, no verification, and
no tunnel process to keep alive:

```bash
npx freestyle@latest tls create \
  --domain mediagrab.style.dev \
  --from public \
  --to vm=<vm-id>,port=8000
```

The service must listen on `0.0.0.0`, not just `localhost`, or the edge cannot
reach it. `deploy/mediagrab.service` already does. To stop serving traffic,
delete the rule — the name stays claimed and can be re-routed later.

**A Cloudflare quick tunnel.** No account needed, but the hostname is **random
and changes on every restart**, which breaks any shared link. Treat it as a
local debugging aid only:

```bash
ENABLE_TUNNEL=1 bash deploy/provision.sh
```

Whichever you choose, set `BEHIND_PROXY=true` so the client IP is resolved from
`CF-Connecting-IP`/`X-Forwarded-For` rather than the socket peer. Behind an
ingress edge every visitor otherwise shares one address, which collapses the
per-IP concurrency limit into a global one.

### Documentation

| Document | Purpose |
|---|---|
| [`PRD.md`](./PRD.md) | Product requirements: users, features, what is explicitly out of scope, open risks |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | How the system works as built: components, request lifecycle, retention, security |
| [`MULTI_PLATFORM_IMPLEMENTATION_PLAN.md`](./MULTI_PLATFORM_IMPLEMENTATION_PLAN.md) | Ambiguity-gate decisions (§1–§1.4) and milestone roadmap |
| [`DESIGN.md`](./DESIGN.md) | Visual design system and tokens |

> **Working with this codebase:** read `PRD.md` and `ARCHITECTURE.md` first.
> `MULTI_PLATFORM_IMPLEMENTATION_PLAN.md` §1 records *why* decisions were made —
> several designs that look arbitrary (session-first gating, retention disabled
> on desktop, purge-on-retrieval rather than on-completion) are deliberate
> reversals of earlier choices, and reverting them reintroduces bugs that were
> found and fixed.

## Configuration

All settings are environment variables (see `app/config.py`). Copy `.env.example` to `.env` for local reference — **`.env` is gitignored and must never be committed.**

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

## YouTube on a hosted (datacenter) IP

YouTube will answer **`Sign in to confirm you're not a bot`** from a datacenter IP. This is address reputation, not configuration — measured on the hosted VM (2026-10):

| Setup | Videos succeeded (of 4) |
|---|---|
| Default (Deno installed, PO-token clients excluded) | 1/4 |
| `--impersonate chrome` | **0/4 — worse** |
| Any of 11 player clients | Identical failures |

An independent published measurement agrees: *16 public videos from a Hetzner address → 0/16; the same binary behind a trusted address → 22/22.*

**MediaGrab already handles everything handleable in code:** the PO-token-dependent `web`/`mweb` clients are excluded, Deno is installed for the player challenge, and the extractor and downloader share one option builder. What remains needs infrastructure:

**Lever 1 — cookies (free, temporary).** Export a logged-out cookie set from a real browser:

```bash
yt-dlp --cookies-from-browser chrome --cookies cookies.txt "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

Then install it on the VM:

```bash
npx freestyle@latest vm scp cookies.txt vm-4ea425a9eae64c4bacd5fde0b2d9a99d:/tmp/
npx freestyle@latest vm exec vm-4ea425a9eae64c4bacd5fde0b2d9a99d -- bash /opt/mediagrab/deploy/install-cookies.sh install /tmp/cookies.txt
npx freestyle@latest vm exec vm-4ea425a9eae64c4bacd5fde0b2d9a99d -- bash /opt/mediagrab/deploy/install-cookies.sh check
```

Installed `0600` root-owned, and **never committed** (`cookies.txt` is gitignored).

> **Limitation:** YouTube binds cookies to the address they came from, so cookies exported from your home network may not authenticate the VM.

**Lever 2 — residential egress (costs money, reliably fixes it).** Set `MEDIA_GRAB_YTDLP_PROXY=socks5://user:pass@host:1080`. The only approach that reliably reaches ~100%, and what commercial download APIs sell.

**Lever 3 — move YouTube work client-side.** The desktop and Android apps download from the user's own residential IP and never hit this. If the bot check proves persistent, steering users to the native apps is a legitimate answer, not a workaround.

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
