# MediaGrab

A lightweight, self-hosted media utility designed to inspect, clip, convert, and download media from over 1,750+ online video and audio platforms (including YouTube, Vimeo, TikTok, SoundCloud, Reddit, X/Twitter) into MP4 videos, MP3 audio tracks, or SRT subtitles.

Runs four ways from one frontend: a **hosted web instance** with guardrails and zero retention, a **local desktop application**, an **Android app**, and the **PWA** in a browser.

---

## Features

- **Four deployment modes:** hosted cloud instance, native desktop app (Windows/Linux), Android app, or browser PWA — all sharing one `/api/` contract.
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

> **Status:** Both the Linux and Windows builds are verified working. The Windows build has been run end to end on a real Windows machine: the app launches, and Facebook and YouTube downloads both complete.
>
> That run was worth more than the confirmation. It exposed five defects that no
> Linux test or CI job could reach — a GUI-subsystem build leaving `sys.stdout`
> as `None`, a browser handing off and causing the launcher to kill its own
> backend, downloads landing inside the app bundle, `CSIDL_PROFILE` used where
> `CSIDL_DOWNLOADS` was meant, and post-processed output that could not be
> located. CI now performs a real download and a no-console launch so that class
> of failure cannot ship again. See `ARCHITECTURE.md` §13 for the full list.

### Build from source

```bash
# Bundle a static FFmpeg (required for merge, MP3, subtitles, trimming)
python packaging/fetch_ffmpeg.py

# Bundle Deno, the JavaScript runtime yt-dlp needs for the player challenge
python packaging/install_deno.py

# Produce dist/MediaGrab/
python -m PyInstaller packaging/MediaGrab.spec --noconfirm --distpath dist
```

Both fetches are optional in the sense that the build still completes without
them, but not in the sense that they are optional in practice: without FFmpeg
most downloads cannot be produced, and without Deno YouTube extraction fails
while every other site keeps working — which looks like a YouTube outage rather
than a missing dependency. `pyinstaller` warns if `deno/` is absent.

### Run it

**Windows:** download `MediaGrab-Windows.zip`, extract it, double-click `MediaGrab.exe`.

**Linux:** download `MediaGrab-Linux-x86_64.tar.gz`, extract it, run `./MediaGrab/MediaGrab`.

No configuration, environment variables, or setup of any kind is required — the launcher sets its own safe defaults, finds your Downloads folder automatically, and bundles FFmpeg and Deno.

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
- **Android is built but not yet released.** CI produces a signed APK once the signing secrets are set; the hosted site keeps pointing Android at the coming-soon page until a release carries one (`DOWNLOADS_PUBLISHED`).

### CI

Two workflows:

- **`tests.yml`** — runs the full pytest suite on every push and pull request, on ubuntu (desktop mode), ubuntu (hosted mode), and Windows. Installs Chromium and Deno so the browser E2E tests and the JS-runtime tests run for real rather than skipping.
- **`build-desktop.yml`** — builds Windows (`.exe`) and Linux (`.tar.gz`) on `v*` tags, smoke-tests the backend and a real download on both runners, verifies the bundle contents, and attaches artifacts to the GitHub release, together with the Android APK. Trigger manually via **workflow_dispatch**.
- **`android.yml`** — Kotlin unit tests and a debug APK on every push and pull request; checks the Android server against the pinned `openapi-contract.json`.
- **`build-android.yml`** — the signed release APK, called by `build-desktop.yml` so the release is published with every platform or none.

---

## Android App

> **Status:** Working on an Android 14 emulator, end to end: paste or share a
> link, choose, download MP4 / MP3 / SRT, and find the file in Downloads. Not
> yet run on a physical phone, and no release has shipped an APK yet.

The same frontend, served by an embedded Kotlin (Ktor) server inside the app
and shown in a WebView. Downloads run on the phone through
[`youtubedl-android`](https://github.com/yausername/youtubedl-android), which
bundles Python, FFmpeg and QuickJS — nothing to install, no server involved,
and YouTube sees the phone's own connection rather than a datacenter.

- **Share → MediaGrab** from YouTube, a browser, or any app that shares a link.
- Files are saved to **Downloads**, de-duplicated as `Title (1).mp4`.
- Downloads **continue in the background**, with a progress notification.
- **arm64 only**, about **56 MB**. Android 8.0 (API 26) or newer.

### Build from source

```bash
bash dev/setup-android.sh            # JDK, Gradle and SDK, all under .toolchain/
source .toolchain/env.sh
(cd android && ./gradlew assembleDebug)
```

The debug APK installs on a phone or an emulator without any key. Release
builds need a signing key from the environment; see
[`dev/README.md`](dev/README.md) for signing, the release check
(`dev/check-apk.py`), and running the app on an emulator.

yt-dlp is pinned in [`android/ytdlp.lock`](android/ytdlp.lock). YouTube breaks
old yt-dlp releases, so expect to bump it.

---

## Web App

### Prerequisites

- Python 3.10+ (tested on Python 3.13 & 3.14)
- `ffmpeg` installed and available on PATH
- Deno on PATH — `python packaging/install_deno.py` fetches one; it is what yt-dlp uses to solve YouTube's player challenge. Without it every other platform still works, which is exactly why its absence is easy to miss. `GET /api/capabilities` reports `js_runtimes.available` so a deployment can be checked rather than guessed at.
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
| [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) | Deploy, release, and incident runbook |

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

**MediaGrab already handles everything handleable in code:** the PO-token-dependent `web`/`mweb` clients are excluded, Deno is installed for the player challenge (by `deploy/provision.sh`, by the Docker image, and bundled into the desktop app — with `js_runtimes.available` in `/api/capabilities` so you can confirm it rather than assume it), and the extractor and downloader share one option builder. What remains needs infrastructure:

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
│       ├── js_runtime.py    # Which configured JS runtimes are actually installed
│       ├── cleaner.py       # Retention daemon & deferred file removal
│       └── templates.py     # Server-side mg-mode injection
├── static/                  # Frontend (PWA shell + hosted gating), shared by every mode
├── android/                 # Android app: Ktor server + youtubedl-android in a WebView shell
│   └── ytdlp.lock           # The yt-dlp version bundled into the APK
├── dev/                     # Android toolchain, emulator sandbox, release check, contract sync
├── tests/                   # pytest suite, incl. headless-Chromium E2E
├── run_desktop.py           # Desktop launcher with window fallbacks
├── packaging/
│   ├── MediaGrab.spec       # PyInstaller build definition
│   ├── fetch_ffmpeg.py      # Static FFmpeg bundler
│   └── install_deno.py      # Static Deno bundler (yt-dlp JS runtime)
├── .github/workflows/
│   ├── tests.yml            # pytest on push + PR: ubuntu desktop, ubuntu hosted, Windows
│   ├── android.yml          # Kotlin tests + debug APK on push + PR
│   ├── build-android.yml    # Signed release APK (called by build-desktop.yml)
│   └── build-desktop.yml    # Windows + Linux + Android release on tags
├── MULTI_PLATFORM_IMPLEMENTATION_PLAN.md  # Architecture, decisions, milestones
├── DESIGN.md                # Design system specification
└── ARCHITECTURE.md          # Architectural specification
```

---

## License

**GNU General Public License v3.0 or later.** Full text in [`LICENSE`](LICENSE); attribution and the third-party component inventory in [`NOTICE`](NOTICE).

MediaGrab was originally MIT-licensed. It is now GPL-3.0-or-later because the
bundled FFmpeg is a GPL build: shipping a GPL binary alongside MIT-licensed code
puts the licensing of the combined distribution in doubt. Rather than resolve
that ambiguity, the project adopted the more restrictive license so the answer
is unambiguous. Every Python dependency is GPL-3.0-compatible; `NOTICE` lists
them with the one exception worth noting, `mutagen`, which is
GPL-2.0-**or-later** and therefore compatible at version 3.

Copies already distributed under MIT remain under MIT — MIT grants are
irreversible and cannot be withdrawn.

Third-party components redistributed in releases:

| Component | License | Why it is bundled |
|---|---|---|
| [FFmpeg](https://ffmpeg.org/) (BtbN static builds) | **GPL-3.0-or-later** | Merge, MP3 conversion, subtitle embedding, trimming. Not optional |
| [Deno](https://deno.com/) | Apache-2.0 | The JavaScript runtime yt-dlp needs to solve YouTube's player challenge |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | The downloader itself |

FFmpeg is invoked as a separate executable process and is not linked into the
MediaGrab source. Releases ship `LICENSE` and `NOTICE` alongside the binaries;
see `docs/OPERATIONS.md` §3.6 for the per-release source-commit record.

> **The Android client (`youtubedl-android`, Milestone 4) is GPL-3.0**, which is
> consistent with the above and introduces no further obligation.
