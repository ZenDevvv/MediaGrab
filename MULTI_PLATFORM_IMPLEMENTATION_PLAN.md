# MediaGrab — Multi-Platform Architecture & Implementation Plan
**Document:** `MULTI_PLATFORM_IMPLEMENTATION_PLAN.md`  
**Status:** Approved & Cleared via Ambiguity Gate
**Amendments:** §1.1 (Milestone 1 review — session-first identity, Android size budget) · §1.2 (Milestone 2 gate — capability sourcing, countdown semantics, locked resolutions).  
**Purpose:** Technical specification, architecture blueprint, and phased execution roadmap for MediaGrab across three platforms:
1. **1-Click Native Desktop App** (Windows & Linux) — Full on-device unlimited capabilities.
2. **Hosted Cloud Instance** (2 vCPU / 4GB RAM / 16GB Disk VM) — Resource-guarded Cloud Lite instance with zero file retention and smart native download promotions.
3. **Standalone On-Device Android App** (`.apk`) — Direct mobile engine (90–120MB APK, ~250–350MB installed) with server-unlimited downloads directly to phone storage.
*(Note: iOS is served strictly as a standard in-browser web client under Cloud Lite rules with zero PWA install prompts).*

---

## 1. Ambiguity Gate Clearance & Decision Record

The following architectural decisions were formally evaluated and confirmed during the Ambiguity Gate audit:

| # | Architecture Dimension | Selected Strategy | Technical Rationale |
|---|---|---|---|
| **1** | **Android API Bridging** | **Embedded Ktor Micro-Server in Kotlin (`127.0.0.1:0`)** | Preserves 100% unified `/api/` fetch and SSE contracts in `app.js` across Web, Desktop, and Android without branching frontend logic. |
| **2** | **Session-First Rate Limiting** | **HTTP 429 — session gate (1) + IP ceiling (3) [Implemented]** | Immediately rejects duplicate concurrent download attempts with an upgrade prompt. Identity resolves to a signed session cookie when present, falling back to IP, so users behind a shared CGNAT NAT no longer block each other. |
| **3** | **Zero-Retention Lifecycle** | **60-Second Post-Retrieval Grace Window** | Protects against browser range requests (`Range: bytes=0-0`), multi-part downloads, and stream reconnects before unlinking. The clock starts at the first `GET /api/file`, not at job completion, so a file on the ready screen is never swept early. Orphan watchdog is 300s. |
| **4** | **Desktop Save Paradigm** | **Direct to `~/Downloads` + App-Mode Fallback** | Local app writes directly to `~/Downloads` with *"Saved to Downloads"* UI. If WebKitGTK is missing on Linux, falls back to Chrome/Chromium `--app` mode. |
| **5** | **CI/CD & Toolchain Strategy** | **GitHub Actions Cloud Builds for Android & Windows** | Android APK (`arm64-v8a`) and Windows `.exe` are compiled on GitHub Actions runners. Local Linux development verified with Python and `uv`. |
| **6** | **Job Cancellation** | **Cooperative cancellation via progress-hook flag** | yt-dlp executes inside a thread executor and cannot be hard-killed. Abort sets a shared `threading.Event` polled by the download and postprocessor hooks; partials are purged on the next hook tick. A true `SIGKILL` requires migrating yt-dlp to a subprocess — tracked as future work. |
| **7** | **Android APK Size Budget** | **90–120 MB APK / ~250–350 MB installed (Revised, see §1.1)** | Supersedes the original ~42 MB estimate. The `youtubedl-android` AAR alone is 56.5 MB before FFmpeg, and native libraries install uncompressed. |

### 1.1 Amendments (Milestone 1 Review)

**Decision #2 revised — session-first identity resolution. [IMPLEMENTED]** The original per-IP-only design was correct in intent but produced unacceptable real-world behavior: consumer and enterprise carriers place many subscribers behind a single CGNAT NAT address, so two unrelated users on the same egress IP would permanently 429 each other even with all 3 slots idle. Resolution now runs:

1. If a valid, HMAC-signed session cookie is present → use the session ID as the identity key.
2. Otherwise → mint a session and fall back to IP for this request.

The IP remains a **secondary** constraint so a client cannot trivially bypass the gate by simply dropping cookies: the per-IP ceiling applies independently and is set to the global concurrency limit, meaning one IP can never occupy more than all 3 slots. The cookie is a fairness mechanism, not an authentication mechanism — it carries no user identity, is server-issued, `HttpOnly` + `SameSite=Lax`, and is only used to disambiguate concurrent-download fairness.

Implementation notes:
- Tokens are `<random-id>.<HMAC-SHA256>`; signatures are compared with `hmac.compare_digest` so a valid signature cannot be recovered by timing. A forged or malformed cookie is discarded and a new session issued.
- The gate key is always the **id half**, never the signed envelope. Mixing the two forms splits one client's occupancy across two gate entries and silently disables the session gate — this was a real defect caught during testing.
- The freshly-minted token gates the *first* request too, otherwise every new client would receive one ungated download before the cookie existed.
- `SESSION_SECRET` is HMAC-keyed. When unset, a per-process key is generated and a warning is logged; sessions then reset on restart, which is acceptable for a fairness device and preferable to a hardcoded key.
- Desktop mode issues no cookie: it is per-user and uncapped, so a cookie would be pointless state.

**Decision #3 — the retention clock starts at retrieval, not completion.** A hosted file is deleted 60 seconds after the user's first `GET /api/file`, not 60 seconds after the job became ready. The original 5-minute post-*serve* rule and a post-completion timer were both wrong in the same way: both can elapse while the user is still sitting on the ready screen, deleting a file they have not saved yet. The purge is therefore scheduled from the retrieval handler alone (`cleaner.deferred_file_removal`), and unclaimed files are exempt from the orphan watchdog regardless of mtime. Desktop disables the whole mechanism — sweeping the user's own Downloads folder is the single most dangerous footgun in this codebase.

**Decision #5 — the duration cap is clip-aware.** A trimmed request produces only the trimmed segment, so the 60-minute cap is evaluated against `metadata.resolve_duration()` rather than the source duration. This is what makes trimming a genuine remedy for a long video rather than a cosmetic control: without it, a 3-hour source could never be downloaded no matter how short the user's clip.

**Decision #6 — abort is cooperative cancellation plus a purge.** yt-dlp runs in a thread executor and cannot be hard-killed, so `POST /api/abort` sets an event that the progress hook observes at its next tick, then purges exactly what that job wrote. On desktop the per-job staging directory makes this precise — it can never touch a concurrent job's fragments. An earlier attempt bounded a `*.part` sweep by mtime; that deleted concurrent downloads, because both are written at the same instant.

**Decision #7 added — Android size budget corrected.** The ~42 MB figure was not achievable. The `youtubedl-android:library` AAR is 56.5 MB and the FFmpeg AAR adds further weight; only `arm64-v8a` is shipped, and native libraries install uncompressed to disk. Budget **90–120 MB APK, ~250–350 MB installed**. Release notes and the store listing must state this honestly rather than promising a 42 MB download.

**Additional M1 findings folded into the spec:**
- `--max-filesize 1024m` is a **coarse backstop only**, not an authoritative size guard. yt-dlp evaluates it per stream, so a merged `bestvideo+bestaudio` output can exceed the cap. The pre-flight estimate in `metadata.validate_request` is the real enforcement; the flag remains as defense in depth.
- The Android WebView cannot load `http://127.0.0.1:{port}/` without a `networkSecurityConfig` permitting cleartext to loopback, and Ktor must serve the bundled `static/` assets in addition to the `/api/` bridge. Both were omitted from the original Component 4 spec and are now specified in §4 Component 4.

### 1.2 Milestone 2 Gate (Frontend)

M2 could not proceed as written: its spec directly contradicted §1.1 Decision #3, and the existing frontend hardcoded the opposite of both. Resolutions below.

**Decision #8 — countdown starts at save click.** §4 and §5 M2 originally specified a "60-second countdown on ready screen" with the notice *"deleted from the server 60 seconds after download."* That predates Decision #3, which moved the grace clock to the first `GET /api/file`. A countdown started at job-ready measures the wrong interval — the file is still alive when it reaches zero. The live countdown now begins **only after the user clicks save**, mirroring the actual purge trigger. The ready screen shows a static, honest notice with no timer.

**Decision #9 — mode from a server-rendered meta tag; limits fail-safe.** `/api/capabilities` is same-origin, served by the same FastAPI app as the page itself, so a hard failure implies the page failed too. The realistic failure is **stale-new/old-backend skew**: `sw.js` can serve a newer `app.js` against a server predating the endpoint, yielding a persistent `404`. Therefore:
  * The server injects `<meta name="mg-mode" content="hosted|desktop">` into `index.html` / `choose.html` at serve time — same origin, zero latency, and structurally incapable of racing the fetch.
  * `/api/capabilities` supplies only numeric limits and download URLs.
  * If that fetch fails or 404s, the UI degrades to **hosted-safe defaults** (max 1080p, no promo strip, no countdown) but the page remains fully usable and the Grab button is never blocked.
  * Cost accepted: a desktop user with a broken capabilities fetch sees chips conservatively gated. This is safe, recoverable, and strictly better than the reverse — in desktop mode the backend applies no guardrails, so failing open would misrepresent capability while failing closed merely mislabels it. On hosted, a transient failure would otherwise advertise 4K that immediately 400s.

**Decision #10 — locked resolutions are disabled, badged, and self-correcting.** `/api/info` returns all resolutions the source offers, including above the hosted cap, since the extractor is mode-unaware. In hosted mode chips above the cap render **disabled with a "Desktop/Android only" badge** — preserving the upsell surface the promo strip leads toward rather than hiding it. If a stored or incoming quality preference is itself locked, the UI **automatically selects the highest permitted option**. Hiding locked resolutions was rejected because it removes the conversion surface; leaving a stale locked selection to 400 server-side was rejected as poor UX.

**Decision #11 — 429 is a first-class UI state.** Post-M1, `429` is a designed outcome carrying a gate-specific message (§4). `app.js` currently collapses every non-OK response into a generic error ("Subtitles unavailable."). The frontend must branch on `429` specifically and surface the server's upgrade prompt verbatim, rather than flattening a conversion opportunity into a dead end.

**Carried-forward observation (resolved):** the ready screen used to call `startDeletionCountdown(30 * 60)` and render *"Deleted from this machine in 29:59."* Wrong in hosted mode (duration, referent, start point) and wrong for desktop, where retention is indefinite. Replaced by Decision #8.

### 1.3 Milestone 3 Gate (Desktop Shell)

M3's "save directly to `~/Downloads`" requirement collides with retention code that is already shipped. Resolutions below.

**Decision #12 — retention is disabled in desktop mode.** The cleaner daemon starts unconditionally in the FastAPI lifespan and sweeps `DOWNLOADS_DIR`, unlinking anything older than `ORPHAN_MAX_AGE_SECONDS` (1800s on desktop). Redirecting `DOWNLOADS_DIR` to `~/Downloads` would therefore have **deleted the user's files after ~30 minutes**. Desktop mode now bypasses the cleaner daemon entirely, and the deferred/purge paths become no-ops. Hosted retention is unchanged.

**Decision #13 — user-facing filenames, with numeric dedupe.** The existing output template is `{job_id}_%(title).%(ext)s`, which would surface as `a19bd4ff_Never Gonna Give You Up.mp4` in the user's Downloads folder. Desktop output uses the bare title; on collision yt-dlp appends ` (1)`, ` (2)`, matching browser download behaviour. The job-id prefix remains in hosted mode, where filenames are ephemeral and never seen twice.

**Decision #14 — desktop suppresses the browser save.** With a server-side write, the frontend must not also navigate to `/api/file/{job_id}`, or the file is written twice. Desktop mode branches on the existing `mg-mode` tag: no navigation, and the ready screen shows *"Saved to Downloads"* with an **Open in Folder** action. Hosted keeps its current browser-driven save.

**Verification scope (superseded):** M3 originally shipped verified on Linux only, with the Windows build explicitly unproven. That run has now happened on the maintainer's Windows VM: the app launches, and Facebook and YouTube downloads both complete. Windows is no longer an unverified claim.

The verification was worth more than the confirmation itself — it exposed five defects no Linux or CI test could reach, each of which had shipped in a released build:

| Defect | Why only a real run found it |
|---|---|
| GUI-subsystem build leaves `sys.stdout` as `None`; the app could not start at all | CI redirects stdio, so `sys.stdout` is never `None` |
| Browser handed off to a running instance; launcher killed its own backend | CI has no browser instance to hand off to |
| Downloads landed in `_internal/downloads` instead of the user's folder | Invisible until a real Downloads folder exists to compare against |
| `CSIDL_PROFILE` (0x28) instead of `CSIDL_DOWNLOADS` (0x05) | Windows shell API, never exercised off-Windows |
| Post-processed output could not be located | CI ran no real download |

The response was structural, not just a set of fixes: CI now performs a real
download and a no-console launch (§1.7 Decisions #27).

### 1.4 Milestone 5 Gate (Hosted Deployment)

**Decision #15 — push-based deployment.** Code reaches the Freestyle VM (`vm-4ea425a9eae64c4bacd5fde0b2d9a99d`, team `acct-00a8ab64d7974b278232fad8bf8b95c0`) via `rsync` from the development host, with a systemd unit managing the service. Freestyle VMs may be ephemeral with no persistent volume, so the deploy must be idempotent and re-runnable: any redeploy is one command and rebuilds from scratch.

**Decision #16 — the promo strip must not ship dead links.** `#hosted-promo-strip` (§1.2 Decision #3) links to `github.com/ZenDevvv/MediaGrab/releases/latest/download/...`. No release exists, so that URL 404s, and Windows binaries cannot be built from the Linux host at all. Deploying before resolving this would advertise a product that fails on click. Until real artifacts exist, the download URLs resolve to an honest "coming soon" destination rather than a 404. This is a **single configuration point** — `GITHUB_RELEASES_URL` — so flipping to real binaries is one value change, not a code edit.

**Deployment checklist:**
- `MEDIAGRAB_MODE=hosted` and a **persistent `SESSION_SECRET`** (an unset value regenerates per process, resetting every user's gate history on each restart)
- `BEHIND_PROXY` set only if traffic genuinely arrives via a trusted proxy
- Zero-retention confirmed (`RETENTION_ENABLED=true`) — the output directory is server-side and must be swept
- Disk budget: 3 concurrent slots × 1 GB cap + overhead must fit the 16 GB volume
- Readiness probe on `/api/capabilities` before traffic is routed

**Credentials.** `FREESTYLE_API_KEY` is not present on the development host; the maintainer supplies it. No credential is ever committed to the repository.

### 1.5 YouTube Posture on the Hosted Instance

Measured on the hosted VM (egress `AS36320 Freestyle Cloud`): YouTube refuses anonymous requests for most videos. Every code-level lever was tried and measured — 11 player clients (identical failures), the Deno JS runtime (no change), TLS impersonation (**0/4, worse than default**), yt-dlp nightly vs stable (1/10 either way), and forced IPv6 egress (no change). A control run from a residential IP succeeded on the same URLs, confirming the cause is address reputation, not configuration.

A cross-platform control test also established that **only YouTube is affected** — SoundCloud, TikTok, Dailymotion, Streamable, TED, Facebook and Vimeo all extract normally from the VM.

**Decision #17 — circuit breaker, not pre-emptive blocking.** A pre-emptive host check would deny the ~1 in 10 videos that currently work, but leaving it fully reactive means *every visitor attempt* becomes another failed YouTube request, which can escalate the address from challenge to hard block. The hosted server therefore probes YouTube **once**, and on the first bot check opens a circuit for 30 minutes (`MEDIA_GRAB_YOUTUBE_COOLDOWN_SECONDS`, default 1800) during which no request reaches YouTube and all callers receive the upsell. Exposure is bounded to roughly one request per window regardless of traffic.

**Decision #18 — the notice is an upsell card, not an error.** The existing `.err-line` treatment reads as failure. The new card reuses the design system's `.limit-banner` vocabulary (accent-soft, 1.5px edge border, 12px radius) with the reason, an app CTA, and a quiet line confirming other sites work. Placement: inline on the landing screen, and on the configure screen when a download is attempted there.

**Decision #19a — recognise every shape of refusal, not just the bot check.** YouTube answers a refused datacenter address in several ways depending on which layer rejects it: a bot check during extraction, a 403 on the media request, a 429 once it has decided the address is a problem. Only the first was recognised, so every other refusal produced a generic error, left the circuit closed, and invited another request. Measured on the hosted instance: metadata succeeds while the transfer is refused 403, so an inspect says nothing about whether a download will work. The retry loop is what turns refusals into the 429 the user then reports, so the fix is to stop the loop, not to add a retry. All refusal shapes now open the circuit for the full cooldown; geo-restriction deliberately does not, because it is a content decision rather than anti-bot and the native app cannot help with it.

**Decision #19 — copy stays truthful.** The notice says *"most links won't work here"*, not *"unavailable"*. Under the circuit breaker some links genuinely do work, and overclaiming is the fastest way to lose a user's trust when they verify elsewhere.

### 1.6 Desktop Save Destination

The desktop app and the hosted site had two different answers to "where does
the file go?", which surfaced as two buttons doing two different things. The
ready screen opened the OS folder (§1.3 Decision #14, because the file is
already written server-side) while the history list offered an HTTP download
that produced a second copy somewhere else entirely.

**Decision #20 — one destination on desktop.** Desktop writes straight to the
user's Downloads folder and both the ready button and every history row reveal
that folder. Hosted is unchanged: the file lives only on the server, so the
primary action is a download that starts the purge clock. The distinction is
now stated in the copy — history rows read "saved to your Downloads folder" on
desktop and "gone in <window>" on hosted.

**Decision #21 — the Downloads folder is the user's, never the app's.**
`configure_downloads_dir()` imported `app.config` before setting
`MEDIA_GRAB_DOWNLOADS_DIR`, which executed the module-level default and cached
it. In the frozen bundle that default is `_internal/downloads`, so every
desktop download landed inside the app itself while the launcher reported the
real folder. Nothing was ever deleted — desktop retention is off by design —
but re-extracting the build orphaned the files. The launcher no longer imports
`app.config` to resolve its override.

**Decision #22 — no retention language on desktop.** The sidebar and drawer
described a purge window in both modes. Hosted has one; desktop has none.
Desktop now says so rather than describing a policy that does not exist.

**Decision #23 — controls that cannot act are hidden, not disabled.** The
"Open Downloads folder" button renders in desktop only: `/api/open-folder`
answers 404 on hosted, and a visible control that visibly does nothing is
worse than no control. The "Embed in the MP4" switch is likewise hidden in
Audio mode, preserving the user's preference across a round trip rather than
clearing it silently.

### 1.7 Settings, Diagnostics & Release Trust

A support request is only actionable if the user can hand over evidence. Until
this section existed the launcher wrote its internals to a stream it had itself
discarded, so a windowed failure produced no artifact at all — the user had
nothing to send and we had nothing to reason from.

**Decision #24 — the settings surface is a left-anchored drawer.** A floating cog
at the lower left of both screens, opening a panel that mirrors the existing
`.drawer-panel` pattern rather than introducing a route. The trigger is on the
left because the panel is; opening a right-hand drawer from a left-hand control
is disorienting.

**Decision #25 — diagnostics offer two routes to the same file.** *Open log file*
reveals `launcher.log` in the OS editor; *Copy to clipboard* puts the tail on the
clipboard for pasting directly into a report. Only the tail is transferred: the
file grows without bound and a failure is always at the end. The clipboard action
falls back to `execCommand`, because a packaged app may not hold clipboard
permission and the whole point is that this always works.

The log path is defined once, in `app/services/logpaths`, shared by the launcher
that writes it and the panel that reveals it. Two copies would drift and the panel
would open the wrong file — the failure would look like the bug is back.

**Decision #26 — update checks compare a real version.** An update check is
meaningless without knowing what is running, and nothing tracked a version:
`app/main.py` carried a stale hardcoded `1.0.0` while releases were at `0.1.x`.
`APP_VERSION` is stamped from the tag at package time and reported through
`/api/capabilities`. A `dev` build skips the comparison and says so rather than
reporting a development build against a production release.

Two details that are easy to get wrong:

- **A failed check must not look like "no update".** If GitHub cannot be reached
  the endpoint answers `503` and the panel says it could not tell. Silently
  reporting "up to date" would be the more comfortable lie and the more dangerous
  one.
- **Unparseable tags sort lowest, never highest.** A malformed release must not
  be able to tell a current user to update.

**Decision #27 — releases are published only after verification.** Build jobs
upload artifacts; a single `publish` job with `needs: [windows, linux]` creates the
release and refuses to publish if any expected asset is missing. This exists
because v0.1.0 published a Linux-only release: each build job created the release
itself, so the Linux job shipped while the Windows job failed, and nobody had to
be told — the download button simply pointed at a build that could not start.

Two smoke tests were added at the same time, each covering a class of failure
the previous suite was structurally unable to see:

- **A no-console launch.** The original smoke test used `Start-Process` with
  redirection, which hands the child real file handles — so `sys.stdout` is never
  `None` and the windowed-stdio crash cannot occur under test. The new step
  launches with no redirection at all, which is what a double-click produces.
- **A real download**, served from a committed fixture over a local HTTP server.
  Startup tests passed happily through three releases in which *every* desktop
  download failed. The first version of this test used YouTube and failed for
  reasons unrelated to the code, because CI addresses trip the same bot check; a
  gate that fails for unrelated reasons is not a gate.

**Decision #28 — desktop-only surfaces are hidden on hosted.** The settings
trigger, the diagnostics group and the update group all ship `hidden` in the markup
and are revealed after the mode is known. Hidden rather than disabled, and hidden
*before* JavaScript runs so they never flash on the public site — the same
fail-closed pattern as the hosted promo strip.

### 1.8 Android Toolchain, Dependency Reality & the Pinned API Contract

Recorded 2026-10-13, immediately before Milestone 4 begins. Three findings from
standing up a real build, each of which changes how the milestone is carried out.

**Decision #29 — `youtubedl-android` is built from source, not resolved as a
dependency.** The coordinate previously recorded here (`io.github.junkfood02.…`)
never existed, and the current one is not obtainable either. The project is
`com.github.yausername:youtubedl-android` on JitPack, and **every version
returns 404**: 0.18.1, 0.18.0, 0.17.4, 0.16.0, 0.15.0, 0.14.0. This is not a
cold cache. JitPack's build log shows a structural failure — its image has no
JDK 17 and the library's own build requests one:

```
Cannot find a Java installation on your machine (Linux … amd64)
matching: {languageVersion=17, vendor=any vendor}
Toolchain download repositories have not been configured.
```

Building the same tag against a local JDK 17 succeeds (`:library:assembleRelease`,
~2m30s, 57 MB AAR). So Milestone 4 uses a Gradle **composite build** — or a
vendored source checkout — not a `implementation(...)` line. Anyone who writes the
dependency line will get a 404 at configure time and may not notice why.

**Decision #30 — the JS runtime on Android is the bundled QuickJS, not Deno.**
Unpacking the AAR shows it ships `jni/arm64-v8a/libqjs.so` (~0.9 MB) alongside
`libpython.zip.so` (~14.3 MB) and `res/raw/ytdlp` (~3.2 MB). QuickJS is what
solves the player challenge, so the ~92 MB Deno bundle added to the desktop app
(§1.1's sibling problem) is **not** carried into the APK. The two platforms solve
the same problem at very different cost; do not copy the desktop answer across.

**Decision #31 — the `/api/` contract is pinned before any Kotlin is written.**
The frontend is one file shared by both servers, so the Kotlin implementation must
answer with byte-identical JSON. Nothing tied the two together, and drift produces
no error anywhere — the WebView simply renders nothing on a phone while desktop
keeps working. The concrete failure is naming: Pydantic emits snake_case
(`job_id`, `download_url`), Kotlin defaults to camelCase, and a `val jobId`
yields `.job_id === undefined` with no stack trace.

`openapi-contract.json` snapshots the document FastAPI already serves at
`/openapi.json`, and `tests/test_api_contract.py` asserts the live spec still
matches. Verified stable across modes, version stamps and download
configuration, so it describes the code rather than the deployment. The Kotlin
side must consume the same file rather than restating the schema.

**Decision #32 — the project is GPL-3.0-or-later, not MIT.** There was no
`LICENSE` file at all; the README's "MIT" conveyed no grant, because MIT requires
the copyright and permission notice in all copies. Meanwhile the bundled FFmpeg is
a GPL build, so shipping it alongside MIT-licensed code left the combined
distribution's licensing in doubt. Rather than resolve that ambiguity, the project
took the more restrictive license. Every runtime dependency is GPL-3.0-compatible;
`mutagen` is the one to watch, being GPL-2.0-**or-later**, whose "or later" clause
is what permits distribution at version 3. Copies already distributed under MIT
remain MIT — MIT grants are irreversible and are not withdrawn.

This also settles the Android obligation above: `youtubedl-android` is GPL-3.0 and
is now simply compatible with the project's own licence. Being correctly licensed
in the repository is necessary but not sufficient, though — GPL-3.0 §4 requires the
licence to accompany the *distributed work*, so `LICENSE` and `NOTICE` are bundled
into the PyInstaller output and CI fails the build if either is missing from the
artifact. The same check applies to the APK.

**Toolchain placement.** The JDK, Gradle, Android SDK, Gradle cache and AVD home
all live under `.toolchain/` (gitignored), installed by `dev/setup-android.sh`.
Nothing is written to `/usr/lib/jvm`, `/usr/bin`, `~/.gradle` or `~/.android`.
This is deliberate: M4 is the first milestone that cannot be verified without a
compiler, and the desktop milestone shipped with CI green before five defects
surfaced that only a real run could find.

---

## 2. Ecosystem Architecture

```mermaid
flowchart TD
    subgraph HostedCloud ["Hosted Cloud VM (2 vCPU / 4GB / 16GB)"]
        direction TB
        CloudServer["FastAPI Cloud Backend\n- 1080p Cap | 60m Dur (Clip-Aware) | 1GB Size\n- 3-Worker FIFO Queue (Session-first: 1/session, 3/IP ceiling -> 429)\n- Zero Retention (60s Post-Retrieval Grace / 300s Orphan Watchdog)"]
        CloudUI["Responsive Web UI\n(Smart Device Detection & Clip-Aware Trimmer)"]
    end

    subgraph DesktopClients ["Local Desktop App (Windows & Linux)"]
        direction TB
        DesktopRunner["run_desktop.py\n(pywebview Native Window / Chrome --app Fallback)"]
        LocalFastAPI["In-Process Uvicorn Thread (127.0.0.1:0)"]
        DesktopEngines["Bundled Python + FFmpeg + yt-dlp\n(Saves directly to ~/Downloads)"]
    end

    subgraph AndroidClient ["Standalone Android App (arm64-v8a)"]
        direction TB
        AndroidAPK["MediaGrab.apk (90-120MB)\n(Foreground Service + MediaStore)"]
        EmbeddedKtor["Embedded Ktor Micro-Server\n(Internal 127.0.0.1:0 REST/SSE Bridge)"]
        AndroidEngine["youtubedl-android Engine\n(Embedded Python + Native FFmpeg .so)"]
    end

    subgraph iOSClient ["iOS Devices (Safari Browser)"]
        iOSBrowser["In-Browser Web Client\n(Cloud Lite Limits · No PWA Prompts)"]
    end

    CloudUI -->|"Windows User"| WinDownload["Download Windows App (.exe)"]
    CloudUI -->|"Linux User"| LinDownload["Download Linux App (Binary)"]
    CloudUI -->|"Android User"| ApkDownload["Download Android App (.apk)"]
    CloudUI -->|"iOS User"| iOSBrowser

    WinDownload -.-> DesktopClients
    LinDownload -.-> DesktopClients
    ApkDownload -.-> AndroidClient
    iOSBrowser --> CloudServer
```

---

## 3. Platform Comparison Matrix

| Dimension | Hosted Cloud Instance | Local Desktop App | Standalone Android APK | iOS Web Users |
|---|---|---|---|---|
| **Target Platforms** | Shared Cloud VM (2 vCPU / 4GB / 16GB) | Windows (.exe) & Linux (Binary) | Android 8.0+ (.apk) | iOS Safari Browser |
| **Execution Engine** | Cloud VM CPU & RAM | User's PC CPU & GPU | User's Phone ARM Processor | Cloud VM CPU & RAM |
| **Max Video Resolution** | **1080p** (2160p & 1440p locked) | **Unlimited** (4K / 8K / 60fps) | **Unlimited** (1080p remux recommended) | **1080p** |
| **Max Duration** | **60 minutes** (Clip-aware; SRT exempt) | **Unlimited** | **Unlimited** | **60 minutes** (Clip-aware; SRT exempt) |
| **Max File Size** | **1.0 GB** (Pre-flight estimation + 1024m guard) | **Unlimited** | **Unlimited** (Phone storage) | **1.0 GB** |
| **Concurrency** | **3 active jobs** (Session-first: 1 per session, 3 per IP ceiling) | **Concurrent** | **Concurrent / Queued** | Shares hosted 3-worker pool |
| **File Storage** | **Zero retention**: purged 60s after first retrieval, tab close (`POST /api/abort`), or 300s orphan sweep | Saved permanently to `~/Downloads` | Saved directly to `/Download` via Android `MediaStore` | Downloaded to Files app; purged on VM |
| **PWA Install Prompts** | Disabled across hosted web (redirects to native app) | N/A | N/A | **Completely disabled** |
| **Distribution** | Hosted VM URL | GitHub Releases (`.exe` / `.tar.gz`) | GitHub Releases & Hosted Site (`.apk`) | Web browser URL |

---

## 4. Detailed Component Architecture

### Component 1: Cloud Backend & Guardrails (`app/`)

#### Configuration (`app/config.py`):
* `MEDIAGRAB_MODE = os.getenv("MEDIAGRAB_MODE", "desktop").lower()` (`"desktop"` or `"hosted"`).
* `BEHIND_PROXY = os.getenv("BEHIND_PROXY", "false").lower() == "true"` (Controls trusted header resolution for real IP extraction).
* Hosted limits:
  * `HOSTED_MAX_RESOLUTION = "1080p"`
  * `HOSTED_MAX_DURATION_SECONDS = 3600` (60 minutes; SRT mode exempt)
  * `HOSTED_MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024 * 1024` (1 GB)
  * `HOSTED_CONCURRENCY = 3` (Global Semaphore)
  * `HOSTED_PER_IP_CONCURRENCY = 3` (Secondary ceiling — one IP can never hold more than the full pool; see §1.1)
  * `HOSTED_PER_SESSION_CONCURRENCY = 1` (Primary gate — the limit users actually feel)
  * `SESSION_COOKIE_NAME = "mg_session"`
  * `SESSION_SECRET` (env; auto-generated at startup with a warning if unset)
  * `HOSTED_POST_DOWNLOAD_GRACE_SECONDS = 60` (Post-retrieval; protects browser range requests/retries)
  * `HOSTED_IDLE_WATCHDOG_SECONDS = 300` (Orphan sweep; must exceed the grace window)
  * `GITHUB_RELEASES_URL = "https://github.com/ZenDevvv/MediaGrab/releases/latest"` (real repo, not a placeholder)
  * `MEDIAGRAB_MODE = os.getenv("MEDIAGRAB_MODE", "desktop")` — the hosted Docker image sets `hosted`.

#### Real IP Resolution (`app/services/network.py` or helper in `app/api/`):
* If `BEHIND_PROXY` is enabled:
  1. Inspect `CF-Connecting-IP` (Cloudflare).
  2. Inspect first address in `X-Forwarded-For`.
  3. Fall back to `request.client.host`.
* If `BEHIND_PROXY` is disabled, strictly use `request.client.host`.

> **Security requirement.** Forwarded headers must never be trusted when the
> origin is directly reachable — otherwise any client forges an address and
> defeats both gates. `BEHIND_PROXY` gates this explicitly and defaults to
> `false`.

#### Session-First Identity Resolution (`app/services/network.py`):
* **Rationale (§1.1):** per-IP-only limiting punishes unrelated users sharing a
  CGNAT NAT. A server-issued session cookie disambiguates them without
  introducing accounts.
* Resolution order for every `/api/download`:
  1. Valid signed session cookie present → identity = session ID.
  2. Otherwise → identity = resolved client IP (per the rules above).
* **Both gates always apply.** The session gate is the tight one (1 active
  download); the IP gate is the anti-evasion ceiling (3 active downloads). A
  client dropping its cookie to reset the session gate still cannot exceed the
  IP ceiling, so the pool cannot be monopolized.
* Session cookies are:
  * server-issued on first `/api/download`, returned via `Set-Cookie`;
  * `HttpOnly` (not readable by JS), `SameSite=Lax`, `Secure` when served over
    HTTPS, `Path=/`;
  * an opaque random identifier (≥128 bits entropy) — **no user identity, no
    PII, no cross-session linkage.** This is a fairness device, not auth;
  * rejected (and regenerated) if the signature does not verify.
* Signing uses an HMAC keyed by `SESSION_SECRET`. If `SESSION_SECRET` is
  unset, the server generates one at startup and logs a warning — sessions then
  reset on restart, which is acceptable for fairness purposes.
* **A 429 response must distinguish which gate tripped** so the UI can show an
  accurate message: a session-limit 429 and an IP-limit 429 read differently.
  The session message is the one quoted in §4 Component 1.

#### Schemas (`app/api/schemas.py`):
* `CapabilitiesResponse`:
  * `mode`: `"desktop"` | `"hosted"`
  * `limits`: Dictionary with `max_resolution`, `max_duration_seconds`, `max_file_size_bytes`, `concurrency_limit`, `per_ip_limit`, `per_session_limit`, `idle_watchdog_seconds`, `locked_resolutions`.
  * `retention_seconds`: post-retrieval grace window (60 hosted / 1800 desktop).
  * `downloads`: Dictionary with `windows_url`, `linux_url`, `android_url`.
* `JobProgressResponse`: includes `queue_position: Optional[int]`, `active_slots: int`.

#### Downloader Service & Guardrails (`app/services/downloader.py` & `app/api/routes.py`):
* **Unqueued Extraction**: `/api/info` remains immediate and outside the download queue. Its result seeds the short-lived metadata cache so `/api/download` can enforce guardrails without a second extraction. (No `/api/extract` endpoint exists — the plan's original mention was removed in M1.)
* **Fair-Share 3-Slot Concurrency Gate**:
  * Global `asyncio.Semaphore(3)`.
  * Session gate: max 1 active download per session ID (falls back to IP when no cookie — see §4 "Session-First Identity Resolution").
  * IP ceiling: max 3 active downloads per resolved IP, applied independently so cookie-dropping cannot evade the gate.
  * If either gate is exceeded: return **HTTP 429 Too Many Requests** with a detail message that names the gate which tripped:
    > *Session gate:* "Only 1 concurrent download allowed per session on hosted server. Please wait for your previous download to finish, or download the desktop/Android app for unlimited concurrency."
    > *IP ceiling:* "Too many concurrent downloads from your network. Please wait for an active download to finish, or download the desktop/Android app for unlimited concurrency."
  * Jobs beyond 3 active slots enter FIFO wait; progress updates stream `"Server busy (3/3 slots). Waiting in queue (Position X)..."`.
* **Clip-Aware Validation**:
  * Mode `srt` is **exempt** from the 60-minute duration cap.
  * If source video duration $\le 60\text{m}$, allowed directly.
  * If source video duration $> 60\text{m}$, extraction succeeds, but `/api/download` validates that trimmed clip duration $(end - start) \le 3600\text{s}$. If untrimmed, returns HTTP 400 guiding the user to trim a segment or download the native app.
* **Pre-Flight Size Estimation & In-Flight Guard**:
  * Evaluates metadata `filesize`, `filesize_approx`, or `tbr * duration`. If estimated size $> 1.0\text{ GB}$, rejects upfront with HTTP 400.
  * Passes `--max-filesize 1024m` to `yt-dlp` as **coarse defense in depth**. Note (§1.1): yt-dlp evaluates this per stream, so a merged `bestvideo+bestaudio` output can still exceed the cap — the pre-flight estimate is the authoritative enforcement, not this flag.
  * A size estimate of *unknown* must **not** be treated as either passing or failing. Unknown passes pre-flight and defers to the in-flight guard.
  * Catches yt-dlp `DownloadError` and returns a clean, user-friendly error message, mapping max-filesize failures to an explicit "download the native app" prompt.
* **Zero-Retention File Engine**:
  * `GET /api/file/{job_id}` schedules deferred removal with `delay_seconds=60` via FastAPI `BackgroundTasks`. **The countdown starts on first retrieval, not on job completion** — a file sitting on the ready screen is never deleted before the user clicks Save.
  * Marking a job `claimed` on that first retrieval hands the file to the orphan watchdog; unclaimed ready files are exempt from the watchdog regardless of mtime.
  * `POST /api/abort/{job_id}` requests cooperative cancellation (§1 Decision #6) and purges partial artifacts.
  * Background cleaner daemon unlinks any unclaimed, unclaimed-by-any-live-job file older than `ORPHAN_MAX_AGE_SECONDS` (300s hosted). Files belonging to in-flight jobs — matched by the `{job_id}_` prefix, since a running download has no `file_path` yet — are always skipped.

---

### Component 2: Frontend UX & Smart Device Detection (`static/`)

#### Smart Device Detection (`static/js/app.js`):
* Mode is read from the server-rendered `mg-mode` meta tag (authoritative, race-free — §1.2 Decision #9). Numeric limits and download URLs come from `GET /api/capabilities`, degrading to hosted-safe defaults on failure.
* Inspects `navigator.userAgent` and `navigator.maxTouchPoints`:
  * **Windows**: Shows *"Get 4K & Unlimited Downloads → [Download Windows App (.exe)]"*.
  * **Linux**: Shows *"Get 4K & Unlimited Downloads → [Download Linux App]"*.
  * **Android**: Shows *"Download directly on your phone → [Download Android App (.apk)]"*.
  * **iOS**: Suppresses all PWA prompts; displays standard Cloud Lite notice with zero install popups.
* **PWA Suppression**: Cancels generic `beforeinstallprompt` on hosted web to prevent confusion with native apps.

#### UI Modifications (`static/index.html` & `static/choose.html`):
* **Top Promo Strip (`#hosted-promo-strip`)**: Sleek banner with platform-targeted download CTA. Includes an "Other Platforms" toggle to view all downloads.
* **Feature Gating**: 1440p and 2160p resolution chips render **disabled with a "Desktop/Android only" badge** in hosted mode — the chips stay visible to preserve the upsell surface. If a stored or incoming quality preference is itself locked, the UI **auto-selects the highest permitted option** (§1.2 Decision #10).
* **Clip-Aware Trimmer UI**:
  * When source video $> 60\text{m}$ and mode is `mp4` or `mp3`, display banner on `/choose`:
    > *"Hosted limit: 60 minutes. Trim a segment $\le$ 60 minutes below, or download the Desktop/Android app for full videos."*
  * The "Grab" button remains disabled until the selected trim range is $\le 60\text{m}$.
  * Mode `srt` shows no trimmer warning and allows grabbing full transcripts.
* **Ready Screen Notice**:
  * In **Hosted Mode**: A static, honest notice with **no countdown**:
    > *"⚠️ Ephemeral Server: This file is deleted from the server shortly after you save it."*
    The live 60-second countdown begins **only when the user clicks save**, mirroring the actual `GET /api/file` purge trigger (§1.2 Decision #8). A countdown shown on the ready screen would reach zero while the file is still alive.
  * In **Desktop Mode**: Displays *"✅ Saved to Downloads"* with an *"Open in Folder"* action, and **no retention timer** — files persist.

---

### Component 3: Local Desktop Shell (`run_desktop.py` & Packaging)

#### Desktop Runner (`run_desktop.py`):
* Binds FastAPI to an ephemeral local port (`127.0.0.1:0`).
* Starts Uvicorn in a daemon thread with `MEDIAGRAB_MODE=desktop`.
* Configures default output directly to user's OS Downloads folder (`Path.home() / "Downloads"`).
* Prepends bundled static FFmpeg directory to `PATH`.
* Spawns native `pywebview` window (Edge WebView2 on Windows, WebKitGTK on Linux).
* **Headless / Missing WebKitGTK Fallback**:
  * If `WebKitGTK` or `pywebview` fails to launch on Linux, automatically detects and spawns Chrome/Chromium in app mode:
    `google-chrome --app=http://127.0.0.1:{port}` / `chromium --app=http://127.0.0.1:{port}`.
  * If no Chromium browser exists, falls back to `webbrowser.open()`.
* Shuts down cleanly on window close.

#### PyInstaller Spec (`packaging/MediaGrab.spec`):
* Bundles Python backend, static assets, and FFmpeg binary into standalone distributions.
* Configures PyInstaller `--onedir` / `--onefile` profiles.

#### GitHub Actions CI (`.github/workflows/build-desktop.yml`):
* Automates Windows `.exe` and Linux binary compilation on git tag releases.

---

### Component 4: Standalone Android App (`android/`)

#### Architecture:
* Located in monorepo directory `android/`.
* **Embedded Ktor Micro-Server**:
  * Spins up an internal Ktor HTTP server on `127.0.0.1:0` in a background coroutine.
  * Exposes identical `/api/info`, `/api/download`, `/api/progress/{job_id}`, and `/api/events/{job_id}` endpoints.
  * Frontend WebView navigates to `http://127.0.0.1:{port}/`, executing unchanged `app.js` logic.
  * **Must also serve the bundled web assets** (`/css`, `/js`, `/icons`, `/`, `/choose`) from the APK's assets directory. The original spec described only the `/api/` bridge, which would leave the WebView with no UI to load.
  * Use the **CIO engine** (`ktor-server-cio`) rather than Netty — CIO is coroutine-based with a much smaller dependency footprint, which matters directly against the size budget in §1.1.
* **Cleartext Loopback Configuration (mandatory)**:
  * Android blocks cleartext HTTP by default on API 28+. The WebView loading `http://127.0.0.1:{port}/` will silently fail without an explicit exception.
  * Ship a `res/xml/network_security_config.xml` scoped **only** to loopback domains (`127.0.0.1`, `localhost`, `::1`) with `cleartextTrafficPermitted="true"`, referenced from the manifest via `android:networkSecurityConfig`.
  * The base config must keep cleartext **disabled** for all other domains — do not set a blanket `usesCleartextTraffic="true"`.
  * Note: from Android 17 (API 37) an implicit localhost config exists, but we still ship the explicit config for the 8.0–16 range.
* **Core Engine**:
  * Integrates `youtubedl-android` (`com.github.yausername:youtubedl-android`), **built from source as a Gradle composite build — not resolved from JitPack, which cannot build it** (§1.8 Decision #29).
  * Initialize both `YoutubeDL.getInstance().init(this)` **and** `FFmpeg.getInstance().init(this)` — without FFmpeg, remux/mp3/subtitle postprocessing silently fails.
  * Pre-compiled FFmpeg shared libraries (`.so`) for `arm64-v8a` ABI only.
  * The AAR is **not** a Kotlin rewrite: it embeds Python (`libpython.zip.so`, ~14.3 MB per ABI) and a prebuilt `yt-dlp` binary (`res/raw/ytdlp`, ~3.2 MB), driven from Kotlin. It also bundles **QuickJS** (`libqjs.so`, ~0.9 MB) as the player-challenge runtime, so Deno is not required here (§1.8 Decision #30).
  * **Size budget: 90–120 MB APK, ~250–350 MB installed** (§1.1, Decision #7). This is the honest figure and must be stated in release notes and any store listing. Supersedes the original ~42 MB / ~85 MB estimates throughout this document.
* **Storage & Services**:
  * Foreground Service with persistent notification to prevent Android OS process kills during active downloads.
  * Completed media moved to `/Download` folder via Android `MediaStore.Downloads` (API 29+) with legacy storage fallback for API 26–28.
  * Silent background `yt-dlp` updates via `YoutubeDL.getInstance().updateYoutubeDL()`.
  * **License obligation:** `youtubedl-android` is GPL-3.0. **Satisfied** — MediaGrab is GPL-3.0-or-later, `LICENSE` and `NOTICE` ship in every release, and the APK's corresponding source is the public repository (§1.8, Decision #32).

#### GitHub Actions CI (`.github/workflows/build-android.yml`):
* Sets up JDK 17 (Temurin) and the Android SDK via `android-actions/setup-android@v4`.
* Compiles `MediaGrab-arm64-v8a.apk` (**budget 90–120 MB**) and attaches it to GitHub Releases.
* Signing keys are supplied from GitHub Secrets (base64-encoded keystore); the keystore must never be committed to the repository.

---

## 5. Implementation Milestones

```mermaid
flowchart LR
    M1["Milestone 1\nDual-Mode Core & Guardrails\n(config, session-first 429, 3-slot queue,\n60m clip guard, post-retrieval retention)"]
    M2["Milestone 2\nFrontend Smart Detection\n(mg-mode meta, promo strip, 60m trimmer banner,\npost-save 60s countdown, 429 UX, no iOS PWA)"]
    M3["Milestone 3\nDesktop Shell & CI\n(run_desktop.py, direct ~/Downloads, WebKit fallback, PyInstaller spec)"]
    M4["Milestone 4\nAndroid App & CI\n(android/ project, Ktor micro-server, MediaStore, GitHub Actions)"]

    M1 --> M2 --> M3 --> M4
```

### Milestone 1: Dual-Mode Backend Core & Guardrails (`app/`)
1. Implement `MEDIAGRAB_MODE`, `BEHIND_PROXY`, and hosted limit constants in `app/config.py`.
2. Implement real IP extraction helper in `app/services/network.py`.
3. ~~Implement session-first identity resolution~~ **[DONE]** HMAC-signed opaque session cookie, session gate (1) + independent IP ceiling (3), both always enforced (§1.1).
4. Add `GET /api/capabilities` returning mode, limits, retention, and release URLs.
5. Implement 3-slot FIFO Semaphore with the dual session/IP gate (HTTP 429, gate-distinguishing detail messages).
6. Implement server-side metadata cache and clip-aware duration validator ($\le 60\text{m}$ trimmed segment allowed; SRT exempt).
7. Implement pre-flight size estimator (unknown = pass-through) and `--max-filesize 1024m` coarse backstop with clean error handling.
8. Implement post-retrieval deferred cleanup, orphan watchdog with in-flight/ready-file exemptions, and `POST /api/abort/{job_id}`.
9. Add automated test suite in `tests/test_capabilities_and_modes.py`.

### Milestone 2: Frontend UX & Smart Detection (`static/`)
1. Server renders `mg-mode` meta tag into `index.html` / `choose.html` at serve time (§1.2 Decision #9).
2. Add capability fetching for numeric limits and download URLs, degrading to hosted-safe defaults on failure — never blocking the Grab button.
3. Add dynamic promo strip (`#hosted-promo-strip`) with OS-tailored CTA and "Other Platforms" switcher.
4. Gate >1080p resolution chips in hosted mode: disabled + "Desktop/Android only" badge, with automatic downgrade of a stale locked preference (§1.2 Decision #10).
5. Implement clip-aware trimmer warning banner and Grab-button validation on `/choose` (hosted mode only).
6. Replace the hardcoded ready-screen countdown with a static notice; start the live 60s countdown only on save click (§1.2 Decision #8).
7. Branch on HTTP `429` to surface the server's gate-specific upgrade prompt verbatim (§1.2 Decision #11).
8. Suppress generic PWA prompts across hosted mode; ensure iOS Safari is clean.
9. Update Playwright/DOM tests in `tests/test_ui_dom.py`.

### Milestone 3: Desktop App Shell & Packaging — ✅ complete
1. Build `run_desktop.py` with port discovery, direct `~/Downloads` target, and WebKitGTK / Chrome `--app` fallback. **Done**, extended with the windowed-stdio guard and the window-lifetime fixes in §1.7 and `ARCHITECTURE.md` §7.
2. Build PyInstaller spec file in `packaging/MediaGrab.spec`. **Done**, including the dynamic `pythonnet`/`clr_loader` collection that static analysis misses.
3. Create `.github/workflows/build-desktop.yml` for Windows `.exe` and Linux release builds. **Done**, with the release gated on verification (§1.7 Decision #27) and version stamped from the tag.
4. Verify execution on Linux **and on Windows**. **Done** — see the superseded verification-scope note in §1.3.

### Milestone 4: Standalone Android App (`android/`) — 🚧 in progress

Scaffold and toolchain landed 2026-10-13: `dev/setup-android.sh` installs JDK 17,
Gradle 8.9 and the Android SDK under `.toolchain/`, and `android/` builds a signed
2.0 MB debug APK. That scaffold contains one placeholder object and exists to prove
the toolchain compiles. See §1.8 for what the first real build revealed.

1. Initialize `android/` project structure with Gradle wrapper and AndroidManifest, `arm64-v8a` only. **Done** as a smoke scaffold.
2. Integrate `youtubedl-android` + FFmpeg, and bundle the web assets into APK assets. **Done** — consumed as a composite build (§1.8 Decision #29), engine behind an `Engine` interface so the HTTP surface is JVM-testable. Debug APK **59 MB** against the 90–120 MB budget, arm64-v8a only.
3. Implement embedded Ktor micro-server (**CIO engine**) bridging `/api/` calls to `YoutubeDL` **and** serving the bundled web assets. **Done** for the `/api/` bridge, asset serving and SSE; `/api/file/{job_id}` streaming and `MediaStore` saving are step 5. Validated against `openapi-contract.json` (§1.8 Decision #31).
4. Add loopback-scoped `network_security_config.xml` so the WebView can load cleartext `127.0.0.1` (§4 Component 4). **Done**, together with what step 2 claimed but had not wired: `static/` is now synced into the APK as `assets/web/` at build time, and a launcher `MainActivity` hosts the WebView. The server is owned by the `Application`, not the activity, so recreation cannot start a second one. It binds a fixed port (47813) with an ephemeral fallback, because the port is part of the page origin and an ephemeral one would empty `localStorage` on every launch. **Run end to end on an emulator** (x86_64 API 34, `dev/android-sandbox.sh`), driving the real WebView over DevTools: paste, chooser, download, ready screen, for MP4, MP3 and SRT. That first run found six defects no JVM test could reach: native libraries left compressed in the APK so Python could not start (`useLegacyPackaging`), SSE frames sent as Kotlin `toString()` rather than JSON, every YouTube file named `watch.*` so a repeat grab reported no file, the MP3 bitrate divided to `0`, MP3s without tags or cover, and raw byte counts where the ready card expects `format_size`. Still open: the ready screen claims the file was saved to Downloads, but it sits in app-private storage, and `/api/file` is a 404 until step 5.
5. Implement Foreground Service and `MediaStore.Downloads` saving. **Done, and run on an emulator (API 34).** Verified there: the file lands in public Downloads; a repeat becomes `Title (1).mp4` and the ready screen shows that name; `/api/file` serves bytes identical to the Downloads copy; "Open in Downloads" opens the system Downloads app; a download started then backgrounded with Home completes under the foreground service, which then stops; and a cancel mid-download leaves no staging, no Downloads entry and no Python process. That run found three more defects, fixed: a refused foreground-service start (Android 12+, app backgrounded) escaped `submit()` as a 500 while the job ran on and saved a file anyway; staging from a killed process was never removed; and the library's progress callback is a percentage, which the engine multiplied by 100. **Open:** the AAR bundles yt-dlp 2025.11.12, which gets HTTP 403 on YouTube streams that desktop's 2026.08.19 fetches from the same network. Finished files are published to public Downloads, as desktop writes them (§1.6): `MediaStore.Downloads` on 29+, the public directory with `WRITE_EXTERNAL_STORAGE` on 26–28. Names are read back after insert, because the provider de-duplicates to `Title (1).mp4`. Private staging is deleted once a job ends, whatever the outcome. `/api/file` streams the published file and never deletes it, since retention is hosted-only. `/api/open-folder` opens the system Downloads app. `DownloadService` (type `dataSync`) holds a progress notification while any job is active and stops itself at zero. `POST_NOTIFICATIONS` (33+) and storage (26–28) are requested at launch rather than mid-download.
6. Verify the APK against the **90–120 MB** budget before release; publish with GPL-3.0 compliance (§1.1, §1.8 Decision #32).
7. Create `.github/workflows/build-android.yml` for automated arm64 APK compilation.

---

## 6. Verification & Testing

**The suite runs in CI on every push and pull request.** `.github/workflows/tests.yml`
runs the full set on ubuntu (desktop mode), ubuntu (hosted mode) and Windows. All
three are load-bearing: hosted is not a superset of desktop — the guardrails, the
per-IP ceiling and the retention sweeper exist only there — and Windows is where
`run_desktop.py` exercises its genuinely platform-specific paths.

This was added late and immediately paid for itself. `build-desktop.yml` is
tag-triggered, so for most of the project's life *nothing* ran pytest in CI. The
first Windows run of the new workflow failed with a `UnicodeDecodeError` from 54
`read_text()` calls that omitted `encoding=` and therefore decoded with `cp1252`
on Windows. The bug was latent and older than the workflow that found it.

Two consequences worth carrying into any future test work:

* Reading repository files in tests must specify `encoding="utf-8"` explicitly.
* Assertions that read files must be checked against the *Windows* locale, not
  just Linux. `LC_ALL=C PYTHONUTF8=0` reproduces it locally.

1. **Automated Unit & Integration Tests**:
   * `uv run pytest tests/test_capabilities_and_modes.py -v`
   * Test hosted rejection for >1080p and >1.0 GB videos.
   * Test clip-aware validation: reject >60m untrimmed, allow >60m trimmed to $\le$ 60m, allow SRT regardless of duration.
   * Test 3-worker FIFO queue transitions and HTTP 429 rejection on duplicate IP download.
   * Test **session-first identity**: two requests with the same session cookie → 429; two requests from different sessions behind the same IP → both accepted; dropping the cookie cannot exceed the IP ceiling of 3.
   * Test tampered/unsigned session cookies are rejected and regenerated.
   * Test unknown size estimates pass pre-flight rather than being rejected or waved through.
   * Test abort endpoint, cooperative cancellation flag, and partial-file purge.
   * Test post-retrieval deferred deletion and the orphan watchdog exemptions (in-flight jobs and unclaimed ready files are never swept; claimed files are).
2. **Manual Verification**:
   * **Hosted Mode**: `MEDIAGRAB_MODE=hosted uv run uvicorn app.main:app --port 8000`
     * Verify OS-tailored promo banner on desktop vs mobile user-agents.
     * Verify trimmer guidance for videos >60m.
     * Verify 60s countdown starts on save click, not on ready.
     * Verify `429` surfaces the gate-specific upgrade prompt rather than a generic error.
     * Verify capabilities fetch failure degrades to hosted-safe defaults without blocking Grab.
   * **Desktop Mode**: `python run_desktop.py`
     * Verify native window / Chrome app opens without server restrictions and saves directly to `~/Downloads`.
