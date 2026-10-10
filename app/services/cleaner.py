import asyncio
import time
from pathlib import Path
from app.config import DOWNLOADS_DIR, ORPHAN_MAX_AGE_SECONDS, CLEANUP_INTERVAL_SECONDS
from app.services.jobs import job_manager

async def deferred_file_removal(file_path: Path, delay_seconds: int = 300):
    """Wait for grace period, then delete file from ephemeral storage."""
    try:
        await asyncio.sleep(delay_seconds)
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        print(f"Deferred deletion error for {file_path}: {e}")

def purge_orphan_files(max_age_seconds: int = ORPHAN_MAX_AGE_SECONDS):
    """Scan downloads directory and delete any files older than max_age_seconds."""
    now = time.time()
    try:
        for item in DOWNLOADS_DIR.iterdir():
            if item.is_file():
                file_age = now - item.stat().st_mtime
                if file_age > max_age_seconds:
                    try:
                        item.unlink()
                    except Exception as e:
                        print(f"Error removing orphan file {item}: {e}")
    except Exception as e:
        print(f"Error scanning downloads directory: {e}")

    # Also clean expired in-memory job records
    job_manager.clean_expired(current_time=now)

async def periodic_cleaner_daemon(interval_seconds: int = CLEANUP_INTERVAL_SECONDS):
    """Runs continuously in background lifespan, purging stale files and jobs."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            purge_orphan_files()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Periodic cleaner error: {e}")
