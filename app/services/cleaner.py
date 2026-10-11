import asyncio
import time
from pathlib import Path

from app import config
from app.config import ORPHAN_MAX_AGE_SECONDS, CLEANUP_INTERVAL_SECONDS
from app.services.jobs import job_manager


async def deferred_file_removal(file_path: Path, delay_seconds: int = None):
    """Delete a file after the post-stream grace window.

    Scheduled from ``GET /api/file`` only, so the countdown starts when the user
    actually begins saving rather than when the job became ready. A file left on
    the ready screen is never yanked out from under them.

    No-op when retention is disabled (§1.3 Decision #12): on desktop the output
    directory is the user's own Downloads folder, and deleting from it would
    destroy files they believe they kept.
    """
    if not config.RETENTION_ENABLED:
        return
    if delay_seconds is None:
        delay_seconds = config.CLEANUP_GRACE_SECONDS
    try:
        await asyncio.sleep(delay_seconds)
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        print(f"Deferred deletion error for {file_path}: {e}")


def _protected_paths() -> tuple:
    """Returns ``(protected_paths, in_flight_prefixes)`` the watchdog must skip.

    Two categories:
      * in-flight jobs, matched by the ``{job_id}_`` filename prefix -- a
        running download has no ``file_path`` yet, so its partials can only be
        recognized by name;
      * files that are ready but not yet retrieved. A user may sit on the ready
        screen indefinitely, and a pure mtime sweep would delete the file out
        from under them.
    """
    protected = set()
    prefixes = set()
    for job_id, job in getattr(job_manager, "_jobs", {}).items():
        in_flight = job.status not in ("ready", "error", "cancelled")
        if in_flight:
            prefixes.add(f"{job_id}_")
        for attr in ("file_path", "srt_path"):
            path = getattr(job, attr, None)
            if path and (in_flight or not job.claimed):
                protected.add(str(Path(path).resolve()))
    return protected, prefixes


def purge_orphan_files(max_age_seconds: int = None):
    """Delete unclaimed files, and always expire in-memory job records.

    In-flight jobs are skipped so the watchdog can never delete a file out from
    under an active download.

    Job-record expiry runs regardless of ``RETENTION_ENABLED``: those live only
    in memory, and skipping them would leak one record per download in a
    long-running desktop session. Only the *file* deletion is gated, because on
    desktop those files belong to the user (§1.3 Decision #12).
    """
    now = time.time()

    if not config.RETENTION_ENABLED:
        job_manager.clean_expired(current_time=now)
        return

    if max_age_seconds is None:
        max_age_seconds = ORPHAN_MAX_AGE_SECONDS
    protected, in_flight_prefixes = _protected_paths()

    try:
        # Resolved at call time rather than imported by value, so the desktop launcher
# can redirect the output directory via MEDIA_GRAB_DOWNLOADS_DIR.
        for item in config.DOWNLOADS_DIR.iterdir():
            if not item.is_file():
                continue
            if item.name.startswith(tuple(in_flight_prefixes)) if in_flight_prefixes else False:
                continue
            if str(item.resolve()) in protected:
                continue
            file_age = now - item.stat().st_mtime
            if file_age > max_age_seconds:
                try:
                    item.unlink()
                except Exception as e:
                    print(f"Error removing orphan file {item}: {e}")
    except Exception as e:
        print(f"Error scanning downloads directory: {e}")

    job_manager.clean_expired(current_time=now)


async def periodic_cleaner_daemon(interval_seconds: int = None):
    """Runs continuously in the background lifespan, purging stale files and jobs.

    The loop itself always runs: even with file retention disabled it still
    expires in-memory job records, which would otherwise accumulate for the
    lifetime of a long desktop session. purge_orphan_files decides what is
    actually deleted (§1.3 Decision #12).
    """
    if interval_seconds is None:
        interval_seconds = CLEANUP_INTERVAL_SECONDS
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            purge_orphan_files()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Periodic cleaner error: {e}")