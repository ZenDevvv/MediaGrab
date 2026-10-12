"""Release download targets.

The desktop builds are produced by CI and published to GitHub releases, but a
release only exists once a tag is pushed. Until then the advertised URLs would
404, and the hosted promo strip would advertise a download that fails on click
(§1.4 Decision #16).

Rather than shipping dead links, an unreleased deployment points every download
target at an honest landing page explaining the current state. Publishing the
binaries is then a single configuration change: set ``GITHUB_RELEASES_URL``.

Availability is tracked per platform rather than as one global switch. CI
builds Windows and Linux but has no Android job, so an all-or-nothing flag
would advertise an APK that does not exist -- exactly the dead link this module
exists to prevent. An unpublished platform keeps pointing at the coming-soon
page.
"""

import os
from pathlib import Path
from typing import Optional

from app import config

__all__ = [
    "DOWNLOADS_AVAILABLE",
    "PUBLISHED_PLATFORMS",
    "asset_url",
    "coming_soon_url",
    "release_urls",
]

# Set to "1" (or GITHUB_RELEASES_URL) once real binaries are published.
DOWNLOADS_AVAILABLE = os.getenv("DOWNLOADS_AVAILABLE", "false").lower() == "true"

# Which platforms actually have a CI build today. Keep in lockstep with the
# jobs in .github/workflows/build-desktop.yml.
_PUBLISHED = os.getenv("DOWNLOADS_PUBLISHED", "windows,linux")

# Filenames match what build-desktop.yml attaches to the release.
_ASSETS = {
    "windows": "windows_url",
    "linux": "linux_url",
    "android": "android_url",
}

_ASSET_FILES = {
    "windows_url": "MediaGrab-Windows.zip",
    "linux_url": "MediaGrab-Linux-x86_64.tar.gz",
    "android_url": "MediaGrab-arm64-v8a.apk",
}

PUBLISHED_PLATFORMS = frozenset(
    p.strip().lower() for p in _PUBLISHED.split(",") if p.strip()
)


def coming_soon_url() -> str:
    return os.getenv("DOWNLOADS_COMING_SOON_URL", "/downloads")


def release_urls() -> dict:
    """Return the download targets advertised to the frontend.

    Only platforms listed in ``DOWNLOADS_PUBLISHED`` get a real release URL;
    everything else falls back to the coming-soon destination, so no code path
    can emit a URL that 404s.
    """
    if DOWNLOADS_AVAILABLE:
        base = config.GITHUB_RELEASES_URL.rstrip("/")
        urls = {}
        for platform, key in _ASSETS.items():
            if platform in PUBLISHED_PLATFORMS:
                urls[key] = f"{base}/download/{_ASSET_FILES[key]}"
            else:
                urls[key] = coming_soon_url()
        return urls
    return {key: coming_soon_url() for key in _ASSETS.values()}