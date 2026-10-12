# MediaGrab — Operations Runbook

**Scope:** deploying the hosted instance, publishing a desktop release, and
diagnosing a user-reported failure.

**Not scope:** architecture (see [`ARCHITECTURE.md`](../ARCHITECTURE.md)) and the
reasoning behind decisions (see
[`MULTI_PLATFORM_IMPLEMENTATION_PLAN.md`](../MULTI_PLATFORM_IMPLEMENTATION_PLAN.md)).

---

## 1. The hosted instance

| | |
|---|---|
| Platform | Freestyle VM |
| VM id | `vm-4ea425a9eae64c4bacd5fde0b2d9a99d` (slug `mediagrab`) |
| Shape | 2 vCPU / 4 GiB / 16 GB, Ubuntu 24.04 |
| App directory | `/opt/mediagrab` |
| Service | `mediagrab.service` → uvicorn on `127.0.0.1:8000` |
| Public URL | `https://mediagrab.style.dev` |
| Config | `/etc/mediagrab.env` (root-owned, `0600`) |
| **No public IP** | ingress is provided by a TLS rule on the platform edge |

### 1.1 Deploying

```bash
./deploy/push.sh
```

Pushes the working tree, restarts the service, and probes readiness. It is
re-runnable and deploys exactly what is in the working tree rather than
whatever is on `origin/main`.

The script stages a tarball, uploads it with `vm scp`, and extracts it in a
second call. It does **not** pipe a tar archive through the CLI: the CLI does not
forward stdin to a piped remote `tar`, which fails with *"Refusing to read
archive contents from terminal"*.

It also deliberately does **not** run `deploy/provision.sh`. Provisioning apt
installs as root and `git clone`s into the app directory when it finds no
`.git`, which both conflicts with this push-based model and fails on a non-empty
tree. Packages and the virtualenv are already provisioned; only the service
needs restarting.

### 1.2 Reaching the VM

`npx freestyle@latest <command>` authenticates from a session persisted in
`~/.freestyle/config.json`. **No environment variable is required** — an absent
`FREESTYLE_API_KEY` does not mean unauthenticated.

```bash
npx freestyle@latest vm list
npx freestyle@latest vm exec mediagrab -- bash -lc "systemctl is-active mediagrab"
npx freestyle@latest vm scp ./file vm-4ea425a9eae64c4bacd5fde0b2d9a99d:/tmp/file
```

Logs:

```bash
npx freestyle@latest vm exec mediagrab -- bash -lc "sudo journalctl -u mediagrab -n 200 --no-pager"
```

### 1.3 Ingress

The app has no public IP, so it needs an ingress path.

**A `*.style.dev` subdomain (in use).** Free and permanent, with a
platform-managed wildcard certificate — no DNS records, no verification, and no
tunnel process:

```bash
npx freestyle@latest tls create --domain mediagrab.style.dev \
  --from public --to vm=vm-4ea425a9eae64c4bacd5fde0b2d9a99d,port=8000
```

The service must listen on `0.0.0.0`; `deploy/mediagrab.service` does.

**A Cloudflare quick tunnel (fallback only).** The hostname is **random and
changes on every restart**, so it breaks any shared link. Useful for local
debugging, not for anything a user has been given a URL to.

```bash
ENABLE_TUNNEL=1 bash deploy/provision.sh
```

If the tunnel is restarted, recover the current URL:

```bash
npx freestyle@latest vm exec mediagrab -- bash -lc \
  "sudo journalctl -u mg-tunnel -n 40 --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1"
```

### 1.4 `BEHIND_PROXY` must be true here

Behind the platform edge, every visitor shares one socket address. With
`BEHIND_PROXY=false` the app resolves the client IP to that address, which
collapses the per-IP concurrency limit into a **global** one — three strangers
would lock each other out. `/etc/mediagrab.env` therefore sets:

```ini
BEHIND_PROXY=true
```

This is safe *only* because the VM has no public IP, so the edge is the sole
path in. If the app is ever exposed directly, this becomes the highest-risk
setting in the system: any client could forge `X-Forwarded-For`.

---

## 2. VM lifecycle — read this before planning around the instance

The platform applies two separate timers, and they are easy to conflate.

| Timer | Value | Effect |
|---|---|---|
| **Idle timeout** | 1800 s | The VM **pauses**. Memory and processes are frozen and preserved; the next inbound request **wakes it**. |
| **Auto-delete** | 2 592 000 s (30 days) | The VM is **deleted**, disk and all, after this much *continuous* idleness. |

Verified behaviour: a paused VM woke on the first inbound HTTPS request, with
no measurable cold-start penalty (0.62 s cold versus 0.62 s warm).

**The 30-day clock is the real risk, not the pause.** A hosted instance that
nobody visits for a month is gone. Regular traffic resets it. If that is
unacceptable, remove the auto-delete:

```bash
npx freestyle@latest vm update mediagrab --set auto-delete-seconds -1
```

---

## 3. Publishing a desktop release

### 3.1 The rule

**Pushing a tag publishes.** The workflow builds on `v*` tags only, and the
release is created by a `publish` job that `needs` both platform builds and
refuses to publish if any expected asset is missing.

Because the release is created by that one job, a failing platform job no longer
produces a half-release. It did once: v0.1.0 shipped a Linux-only release
because each build job created the release itself.

### 3.2 Procedure

```bash
./deploy/push.sh                       # hosted instance first, so the site matches
git tag -a v0.1.9 -m "MediaGrab v0.1.9 ..."
git push origin v0.1.9
gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId')
```

Verify before announcing:

```bash
gh release view v0.1.9 --json assets -q '.assets[] | "\(.name) \(.size)B"'
curl -sIL -o /dev/null -w '%{http_code}\n' \
  https://github.com/ZenDevvv/MediaGrab/releases/latest/download/MediaGrab-Windows.zip
```

`releases/latest` is what `downloads.py` advertises, so the site's buttons move
to the new build with no configuration change.

### 3.3 Check the stamped version in the log

```bash
gh run view <id> --log | grep 'build\] version'
```

Both platforms must print the same tag. **A silent difference means a build is
unversioned and its update check will report "development build" forever** — this
happened when `${GITHUB_REF_NAME}` was read in PowerShell instead of bash. The
build now fails on an empty version rather than shipping one.

### 3.4 What the CI smoke tests actually cover

| Test | Catches |
|---|---|
| Backend starts (piped) | Boot and import failures |
| **No-console launch** | The windowed-stdio crash. A *redirected* launch hands the child real handles, so `sys.stdout` is never `None` and the bug cannot appear under test |
| **A real download** | Output-location, template and post-processing defects. Uses a committed fixture over a local HTTP server — an earlier version used YouTube and failed because CI addresses trip the same bot check |

Startup tests alone passed through three releases in which every desktop
download was broken.

### 3.5 Moving a tag

Occasionally a tag must be replaced — a broken build, or one that shipped a
defect. Deleting the release also deletes the tag:

```bash
gh release delete v0.1.9 --yes --cleanup-tag
git tag -d v0.1.9
git tag -a v0.1.9 -m "..." && git push origin v0.1.9
```

Do this only for a release that is minutes old and has no consumers. Once a tag
is public and in use, cut a new version instead — the existing tag keeps
pointing at the old commit, which is what makes it trustworthy.

---

## 4. Advertising downloads

Download targets are per-platform, because CI builds Windows and Linux but has no
Android job:

```ini
DOWNLOADS_AVAILABLE=true      # master switch: master-switch off => every target is /downloads
DOWNLOADS_PUBLISHED=windows,linux   # platforms that genuinely have an asset
```

An unlisted platform resolves to `/downloads` rather than advertising an asset
that cannot exist. Adding an Android build means adding its asset name to
`downloads.py` **and** listing `android` here.

---

## 5. Triage: a user reports something wrong

### 5.1 Desktop: always ask for the log

A windowed app has no console, so nothing is visible unless it is logged. The
settings panel (cog, lower left) has **Open log file** and **Copy to clipboard**,
which is why the user should paste it rather than describe the symptom.

| Platform | Log location |
|---|---|
| Windows | `%LOCALAPPDATA%\MediaGrab\launcher.log` |
| Linux | `~/.mediagrab/MediaGrab/launcher.log` |
| macOS | `~/Library/Application Support/MediaGrab/launcher.log` |

In PowerShell:

```powershell
Get-Content "$env:LOCALAPPDATA\MediaGrab\launcher.log"
```

The log records the launcher's own status lines and job failure tracebacks, plus
a watchdog line while the window is open:

```
[MediaGrab] Backend ready at http://127.0.0.1:65241
[MediaGrab] 09:12:04 backend reachable
[MediaGrab] watchdog: backend thread has exited
```

That last distinction is what the watchdog is for:

| Log says | Means |
|---|---|
| `backend reachable` throughout, no exit line | The backend is fine — the window could not route loopback. Check for a system proxy. |
| `backend thread has exited` | The backend died; the traceback above it is the cause. |
| `backend UNREACHABLE` | The socket stopped answering while the thread lived. |

### 5.2 Hosted: start with the service logs

```bash
npx freestyle@latest vm exec mediagrab -- bash -lc \
  "sudo journalctl -u mediagrab -n 300 --no-pager"
```

### 5.3 Common symptoms

| Reported | First thing to check |
|---|---|
| *"127.0.0.1 refused to connect"* in the app window | Ask for the log and read the watchdog line (§5.1) |
| *"Processing failed: Output media file was not created"* | Desktop-only. Check that downloads resolve after post-processing (`ARCHITECTURE.md` §7.3) |
| *"Too many requests" on a YouTube link | Check the circuit breaker state (§6) |
| *"That link didn't work" on a working URL | Check whether the video is longer than the hosted cap, and whether the session or IP gate is full |
| Downloads missing after a restart | Confirm `DOWNLOADS_PUBLISHED` includes the platform, and that the file is in the real Downloads folder rather than inside the app bundle |

### 5.4 Reproducing a desktop-only bug

CI is not sufficient. Roughly every defect found in the Windows verification run
was structurally invisible to it:

- a GUI-subsystem build leaves `sys.stdout` as `None` — CI redirects stdio
- a browser hands off to a running instance — CI has no browser instance
- downloads land in `_internal/downloads` — needs a real Downloads folder
- `CSIDL_PROFILE` instead of `CSIDL_DOWNLOADS` — Windows shell API
- post-processed output could not be located — CI ran no real download

Anything that depends on the OS shell, an installed runtime, a browser process,
or real filesystem layout needs a real run.

---

## 6. The YouTube circuit breaker

The hosted egress address is refused by YouTube for most videos (§1.5 of the
plan). The server therefore makes **one** outbound YouTube request per cooldown
window and answers everything else locally.

```ini
MEDIA_GRAB_YOUTUBE_COOLDOWN_SECONDS=1800
```

Observing state:

```bash
npx freestyle@latest vm exec mediagrab -- bash -lc \
  "sudo /opt/mediagrab/venv/bin/python -c \"
import sys; sys.path.insert(0,'/opt/mediagrab')
from app.services.youtube_circuit import circuit
print('open:', circuit.is_open(), 'remaining:', round(circuit.remaining()), 'trips:', circuit.trip_count)\""
```

State is per-process and is not persisted: restarting the service resets the
breaker, which costs one probe. That is the safe direction, but it is worth
knowing before scheduling a restart near a traffic peak.

**Do not respond to a YouTube failure by retrying.** Retry loops against a
refusing address are what escalate a challenge into a hard block. The breaker
exists so that nothing needs to.

---

## 7. Runbook of known-bad actions

| Don't | Because |
|---|---|
| Retry a failing YouTube URL "just once more" | Each retry is another request from an address YouTube already refuses |
| Move the VM's Downloads folder by hand | It is the user's folder; the retention daemon is disabled there on purpose |
| Pipe a tarball through the CLI to the VM | Stdin is not forwarded to a piped remote `tar` |
| Add `DOWNLOADS_AVAILABLE=true` without checking the release exists | Advertises a 404 on click |
| Set `BEHIND_PROXY=true` on an instance with a public IP | Any client can forge its IP and bypass both gates |
| Force a git tag after it has been public and used | The old tag keeps pointing at the old commit; cut a new version instead |