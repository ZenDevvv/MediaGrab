from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
# Desktop builds override this to the user's real Downloads folder via
# MEDIA_GRAB_DOWNLOADS_DIR (set by run_desktop.py).
DOWNLOADS_DIR = Path(os.getenv("MEDIA_GRAB_DOWNLOADS_DIR") or (BASE_DIR / "downloads"))
STATIC_DIR = BASE_DIR / "static"

# Ensure downloads directory exists
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# --- Execution Mode -------------------------------------------------------
# "desktop" = unlimited, files retained on the user's own machine.
# "hosted"  = shared cloud VM, guardrails enforced, zero file retention.
MEDIAGRAB_MODE = os.getenv("MEDIAGRAB_MODE", "desktop").lower()
IS_HOSTED = MEDIAGRAB_MODE == "hosted"

# Deletion is a hosted-only behaviour (§1.3 Decision #12). On desktop the
# output directory is the user's own Downloads folder, and sweeping it would
# destroy files the user believes they have kept.
RETENTION_ENABLED = os.getenv("RETENTION_ENABLED", "false" if not IS_HOSTED else "true").lower() == "true"

# When the server sits behind a CDN/reverse proxy, trust forwarded IP headers.
# Leave disabled when exposed directly, otherwise clients can spoof their IP
# and defeat the per-IP concurrency gate.
BEHIND_PROXY = os.getenv("BEHIND_PROXY", "false").lower() == "true"

# --- Ephemeral File Lifecycle --------------------------------------------
# Post-stream grace: the purge countdown starts at the first GET /api/file,
# not when the job becomes ready, so a file is never yanked off the ready
# screen while the user is still looking at it.
CLEANUP_GRACE_SECONDS = int(os.getenv(
    "CLEANUP_GRACE_SECONDS", "60" if IS_HOSTED else "1800"
))
# Orphan watchdog sweeps files nobody ever claimed. Kept well above the grace
# window so it can never race a ready-screen file the user is about to save.
# Unused when RETENTION_ENABLED is false.
ORPHAN_MAX_AGE_SECONDS = int(os.getenv(
    "ORPHAN_MAX_AGE_SECONDS", "300" if IS_HOSTED else "1800"
))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "60" if IS_HOSTED else "300"))

# Desktop writes straight to the user's Downloads folder, so filenames must be
# user-facing and de-duplicated rather than carrying an internal job id
# (§1.3 Decision #13).
USER_FACING_FILENAMES = os.getenv(
    "USER_FACING_FILENAMES", "false" if IS_HOSTED else "true"
).lower() == "true"

# In-memory job records outlive their files slightly so late /api/file range
# requests still resolve to a clean 404 rather than a 500.
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "900" if IS_HOSTED else "1800"))

# --- Execution constraints ------------------------------------------------
# 0 means "no cap" (desktop mode is per-user and self-hosted).
HOSTED_CONCURRENCY = int(os.getenv("HOSTED_CONCURRENCY", "3"))
# Primary gate: what a user actually experiences. Keyed to a session cookie when
# present so unrelated users behind one CGNAT NAT do not block each other.
HOSTED_PER_SESSION_CONCURRENCY = int(os.getenv("HOSTED_PER_SESSION_CONCURRENCY", "1"))
# Secondary ceiling, applied independently. Without it, dropping the cookie
# would reset the session gate and let one client monopolize the pool.
HOSTED_PER_IP_CONCURRENCY = int(os.getenv("HOSTED_PER_IP_CONCURRENCY", "3"))

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "0" if not IS_HOSTED else str(HOSTED_CONCURRENCY)))
PER_IP_CONCURRENCY = int(os.getenv(
    "PER_IP_CONCURRENCY", "0" if not IS_HOSTED else str(HOSTED_PER_IP_CONCURRENCY)
))
PER_SESSION_CONCURRENCY = int(os.getenv(
    "PER_SESSION_CONCURRENCY", "0" if not IS_HOSTED else str(HOSTED_PER_SESSION_CONCURRENCY)
))

# --- Sessions (§1.1 Decision #2) ------------------------------------------
# A fairness device for the concurrency gate, NOT authentication: the token is an
# opaque random ID carrying no user identity, PII, or cross-session linkage.
# Signed with HMAC-SHA256 so a client cannot forge a session to shed its
# history. If SESSION_SECRET is unset, one is generated per process -- sessions
# then reset on restart, which is acceptable for fairness purposes.
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "mg_session")
SESSION_COOKIE_MAX_AGE = int(os.getenv("SESSION_COOKIE_MAX_AGE", str(24 * 60 * 60)))
SESSION_SECRET = os.getenv("SESSION_SECRET") or None
SESSION_SECRET_IS_EPHEMERAL = SESSION_SECRET is None

# --- Hosted guardrails ----------------------------------------------------
HOSTED_MAX_RESOLUTION = os.getenv("HOSTED_MAX_RESOLUTION", "1080p")
HOSTED_MAX_DURATION_SECONDS = int(os.getenv("HOSTED_MAX_DURATION_SECONDS", "3600"))
HOSTED_MAX_FILE_SIZE_BYTES = int(os.getenv(
    "HOSTED_MAX_FILE_SIZE_BYTES", str(1 * 1024 * 1024 * 1024)
))
HOSTED_MAX_FILESIZE_ARG = os.getenv("HOSTED_MAX_FILESIZE_ARG", "1024m")

# Resolutions locked to the native apps in hosted mode.
HOSTED_LOCKED_RESOLUTIONS = ["1440p", "2160p"]

# Short-lived cache so /api/download can re-use the /api/info probe instead of
# paying a second yt-dlp extraction on every request.
METADATA_CACHE_TTL_SECONDS = int(os.getenv("METADATA_CACHE_TTL_SECONDS", "300"))
METADATA_CACHE_MAX_ENTRIES = int(os.getenv("METADATA_CACHE_MAX_ENTRIES", "128"))

# --- YouTube extraction ---------------------------------------------------
# Player clients, tried in order. Deliberately excludes "web" and "mweb":
# both now require a Proof-of-Origin (PO) token for GVS format requests and
# fail outright with "Requested format is not available" when one is absent.
# Listing them wastes a round trip and can mask a working fallback.
#
# Measured on a datacenter IP (2026-10): default and tv_embedded return the full
# format list; android_vr is a cheap last resort with fewer formats.
YTDLP_PLAYER_CLIENTS = os.getenv(
    "YTDLP_PLAYER_CLIENTS", "tv_embedded,default,android_vr"
).split(",")

# Cookies file for YouTube. On a datacenter IP YouTube increasingly answers
# "Sign in to confirm you're not a bot" regardless of client or JS runtime --
# that is IP reputation, and only a logged-out cookie set from a real client
# session works around it. Mounted at MEDIA_GRAB_YTDLP_COOKIES; never committed.
YTDLP_COOKIES_PATH = os.getenv("MEDIA_GRAB_YTDLP_COOKIES") or None

# Egress proxy for YouTube requests. This is the only lever that fixes the bot
# check at the root: YouTube scores datacenter address ranges as a whole.
# Measured 2026-10 on the hosted VM: 1 of 4 videos succeeded without a proxy,
# 0 of 4 with TLS impersonation enabled (worse), and the failures are
# identical across all 11 player clients. Point this at a residential or
# otherwise trusted exit and success goes to ~100%.
#
# Example: socks5://user:pass@host:1080  (also supports http/https).
YTDLP_PROXY = os.getenv("MEDIA_GRAB_YTDLP_PROXY") or None

# TLS fingerprint impersonation. Measured WORSE than the default on a
# datacenter IP (0/4 vs 1/4), so it is off by default. Leave unset unless you
# have measured it as a win on your own egress.
YTDLP_IMPERSONATE = os.getenv("MEDIA_GRAB_YTDLP_IMPERSONATE") or None

# yt-dlp deprecated extraction without a JavaScript runtime for solving the
# player challenge. Deno is the default-enabled runtime.
YTDLP_JS_RUNTIMES = os.getenv("YTDLP_JS_RUNTIMES", "deno").split(",")

# --- Distribution ---------------------------------------------------------
GITHUB_RELEASES_URL = os.getenv(
    "GITHUB_RELEASES_URL", "https://github.com/ZenDevvv/MediaGrab/releases/latest"
)

# --- Network --------------------------------------------------------------
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))