"""Release download targets.

The desktop builds are produced by CI and published to GitHub releases, but a
release only exists once a tag is pushed. Until then the advertised URLs would
404, and the hosted promo strip would advertise a download that fails on click
(§1.4 Decision #16).

Rather than shipping dead links, an unreleased deployment points every download
target at an honest landing page explaining the current state. Publishing the
binaries is then a single configuration change: set ``GITHUB_RELEASES_URL``.

Availability is tracked per platform rather than as one global switch. CI now
builds the Android APK with every release, but no published release carries one
yet, so an all-or-nothing flag would still advertise an APK that does not exist
-- exactly the dead link this module exists to prevent. An unpublished platform
keeps pointing at the coming-soon page; add "android" to DOWNLOADS_PUBLISHED once
a release includes the APK.
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

# Which platforms have a binary in the published release. Keep in lockstep with
# what has actually been released, not just what CI can build.
_PUBLISHED = os.getenv("DOWNLOADS_PUBLISHED", "windows,linux")

# Filenames match what build-desktop.yml attaches to the release; a test reads
# the workflow to keep them that way. The Android name was once
# "MediaGrab-arm64-v8a.apk" here while CI published "MediaGrab-Android-arm64.apk"
# -- a 404 waiting for the day Android was marked published.
_ASSETS = {
    "windows": "windows_url",
    "linux": "linux_url",
    "android": "android_url",
}

_ASSET_FILES = {
    "windows_url": "MediaGrab-Windows.zip",
    "linux_url": "MediaGrab-Linux-x86_64.tar.gz",
    "android_url": "MediaGrab-Android-arm64.apk",
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