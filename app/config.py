from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"

# Ensure downloads directory exists
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Ephemeral File Lifecycle
CLEANUP_GRACE_SECONDS = int(os.getenv("CLEANUP_GRACE_SECONDS", "300"))      # 5 minutes
ORPHAN_MAX_AGE_SECONDS = int(os.getenv("ORPHAN_MAX_AGE_SECONDS", "1800"))   # 30 minutes
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300")) # 5 minutes

# Execution constraints
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))

# Network
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
