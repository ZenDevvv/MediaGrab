"""Release download targets.

The desktop builds are produced by CI and published to GitHub releases, but a
release only exists once a tag is pushed. Until then the advertised URLs would
404, and the hosted promo strip would advertise a download that fails on click
(§1.4 Decision #16).

Rather than shipping dead links, an unreleased deployment points every download
target at an honest landing page explaining the current state. Publishing the
binaries is then a single configuration change: set ``GITHUB_RELEASES_URL``.
"""

import os
from pathlib import Path
from typing import Optional

from app import config

__all__ = ["DOWNLOADS_AVAILABLE", "asset_url", "coming_soon_url", "release_urls"]

# Set to "1" (or GITHUB_RELEASES_URL) once real binaries are published.
DOWNLOADS_AVAILABLE = os.getenv("DOWNLOADS_AVAILABLE", "false").lower() == "true"

# Filenames match what build-desktop.yml attaches to the release.
_ASSETS = {
    "windows_url": "MediaGrab-Windows.zip",
    "linux_url": "MediaGrab-Linux-x86_64.tar.gz",
    "android_url": "MediaGrab-arm64-v8a.apk",
}


def coming_soon_url() -> str:
    return os.getenv("DOWNLOADS_COMING_SOON_URL", "/downloads")


def release_urls() -> dict:
    """Return the download targets advertised to the frontend.

    Falls back to the coming-soon destination whenever the binaries have not
    been published, so no code path can emit a URL that 404s.
    """
    if DOWNLOADS_AVAILABLE:
        base = config.GITHUB_RELEASES_URL.rstrip("/")
        return {key: f"{base}/download/{asset}" for key, asset in _ASSETS.items()}
    return {key: coming_soon_url() for key in _ASSETS}