# MediaGrab — Product Requirements Document

**Version:** 1.0
**Status:** Living document. Records what the product is, who it is for, and
what it deliberately is not.

**Companions:** [`ARCHITECTURE.md`](./ARCHITECTURE.md) (how it works) ·
[`MULTI_PLATFORM_IMPLEMENTATION_PLAN.md`](./MULTI_PLATFORM_IMPLEMENTATION_PLAN.md)
(decisions & milestones) · [`DESIGN.md`](./DESIGN.md) (visual system)

---

## 1. Problem

People need to pull a specific clip, audio track, or transcript out of a video
hosted somewhere — for editing, archiving, study, or sharing. Existing tools are
either desktop-only, browser-only, or demand a compromise on quality or file
size.

Three specific failures recur:

1. **Browser download limits** on hosted converters (resolution caps, size caps,
   duration caps) with no explanation and no way out.
2. **Install friction** on desktop apps — a runtime, a compiler, and a separate
   FFmpeg install before anything works.
3. **Over-installation.** A user who wants one clip should not have to commit to
   software that lives on their machine.

---

## 2. Product statement

> MediaGrab inspects, clips, converts, and saves media from 1,750+ platforms,
> in a browser or as a native app, with a single consistent interface.

---

## 3. Users

| Persona | Needs | Delivered by |
|---|---|---|
| **Casual browser user** (iPhone, work laptop) | Paste a link, get a clip. No install. | Hosted web, iOS browser |
| **Content creator** (Windows, power user) | 4K, no size cap, no time cap, no per-job throttle | Desktop app |
| **Mobile media saver** (Android) | Save directly to the phone, off the network | Android app |
| **Self-hoster** | Run their own instance with their own limits | Docker image |

The hosted server is deliberately **not** trying to be unlimited. It is the
on-ramp. The native apps are the destination, and the promo strip is the
migration path between them.

---

## 4. Product principles

1. **Honest about limits.** The server states its caps plainly and says which app
   removes them. No silent failures, no timers that lie.
2. **Nothing is deleted that the user believes they have.** Hosted deletes on a
   schedule it discloses; desktop never deletes at all.
3. **Zero configuration for non-technical users.** Download, extract, run. No
   environment variables, no prompts, no dependency installation.
4. **Remux over transcode.** Copy the stream when possible; it is faster,
   cooler, and lossless.
5. **One contract, three deployments.** The frontend never branches on platform
   beyond reading a mode flag.

---

## 5. Core features

| # | Feature | Requirement |
|---|---|---|
| F1 | **Inspect** | Metadata + per-resolution size estimate before committing to a download |
| F2 | **Precision trim** | Keyframe slicing (`download_ranges`), not whole-stream download |
| F3 | **MP4 remux** | `-c copy` for AVC1/AAC; no quality loss, minimal CPU |
| F4 | **MP3 extraction** | 320/192/128 kbps, ID3 tagging, embedded cover art |
| F5 | **SRT transcripts** | Skip media entirely; captions in seconds |
| F6 | **Live progress** | SSE telemetry: percent, speed, ETA, stage |
| F7 | **Duplicate subtitles** | Optional sidecar `.srt` alongside the media |
| F8 | **Smart device detection** | OS-tailored download CTA on the hosted instance |
| F9 | **Dual-mode capabilities** | Frontend learns real limits from the server, not hardcoded |
| F10 | **Local history** | `localStorage`; no account, no sync, no telemetry |

---

## 6. Functional requirements

### Hosted mode

| ID | Requirement | Verified by |
|---|---|---|
| H1 | Cap video resolution at 1080p | `validate_request` |
| H2 | Cap clip duration at 60 min; SRT exempt | clip-aware validator |
| H3 | Cap file size at 1 GB, estimated server-side | pre-flight estimator |
| H4 | 3 concurrent slots, FIFO beyond that | scheduler |
| H5 | 1 concurrent download per session; 429 with an upgrade prompt | session gate |
| H6 | Independent per-IP ceiling (anti-evasion) | IP gate |
| H7 | Delete files 60 s after the user retrieves them | deferred removal |
| H8 | Sweep unclaimed files after 300 s | orphan watchdog |
| H9 | Never sweep a file an in-flight job is writing | prefix exclusion |
| H10 | Show a device-tailored native-app prompt | `#hosted-promo-strip` |

### Desktop mode

| ID | Requirement | Verified by |
|---|---|---|
| D1 | Unlimited resolution, duration, and size | no guardrails applied |
| D2 | Save directly to the OS Downloads folder, bare filename, auto-deduped | `USER_FACING_FILENAMES`; folder resolution verified in `logpaths` ordering |
| D3 | Never delete user files | `RETENTION_ENABLED=false` |
| D4 | Zero configuration; no env vars, no prompts | launcher defaults |
| D5 | Window fallback chain if the native window cannot open | launcher |
| D6 | Bundle FFmpeg so postprocessing works on a clean machine | `fetch_ffmpeg.py` |

---

## 7. Non-functional requirements

| ID | Requirement | Status |
|---|---|---|
| N1 | No database | Met — in-memory jobs, `localStorage` history |
| N2 | No frontend build step | Met — vanilla ES6 |
| N3 | Guardrails not spoofable | Met — server-computed from own metadata |
| N4 | Test suite green in both modes | Met — 230 tests |
| N5 | Real-browser UI coverage | Met — headless Chromium via CDP |
| N6 | < 1 s local page load | Unmeasured |
| N7 | Windows binary verified | **Met** — run end to end on a real Windows machine; Facebook and YouTube downloads both complete |
| N8 | Licence obligations satisfied before public distribution | **Not met** — GPL-3.0 FFmpeg unresolved |

---

## 8. Explicitly out of scope

- **No accounts, no sync, no history server-side.** History is local to the device.
- **No batch/playlist downloading.** `noplaylist` is enforced.
- **No scheduled or recurring downloads.**
- **No upload/transcode-to-cloud.**
- **No DRM circumvention.** Only media the source serves publicly.
- **No macOS desktop app.** No PyInstaller cross-build exists for it and none is planned.

---

## 9. Success criteria

**Hosted instance**
- A first-time visitor can go from URL to saved file without instructions.
- Zero 4xx on the promo strip. *(Currently satisfied by the `/downloads` landing
  page until binaries are published.)*
- Zero user files lost. *(The regression that motivated Decision #12.)*
- A user who hits a limit understands why and what would remove it.

**Desktop app**
- Download, extract, run. Nothing else.
- A saved file survives indefinitely and is named cleanly.
- A non-technical user never encounters a configuration prompt.

---

## 10. Open risks

| Risk | Impact | Status |
|---|---|---|
| Windows binary never manually verified | Users get a broken download | **Open** — CI builds it; maintainer has a Windows VM |
| No installer; extraction required | Friction vs. a true one-click install | Accepted for now |
| GPL-3.0 bundled FFmpeg | Cannot publish binaries without obligations | **Open** — blocks distribution |
| Session rate limiting untested under real NAT | Shared-IP users may still collide | **Open** — needs multi-user staging |
| Hosted instance never run multi-user | First real deploy will surface unknown bugs | **Open** |
| `BEHIND_PROXY` misconfigured | Rate limit trivially bypassed | Mitigated by documentation; unenforced |

---

## 11. Success definition

MediaGrab is successful when a user can get exactly the clip they asked for,
from any of 1,750+ platforms, in under a minute, without installing anything —
and without being misled about what the software will do with their file.