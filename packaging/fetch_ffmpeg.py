#!/usr/bin/env python3
"""Fetch a static FFmpeg build for the current platform.

Bundled so the desktop app works out of the box on a machine with no FFmpeg
installed. FFmpeg is required for merging, MP3 conversion, subtitle embedding
and trimming -- without it most downloads cannot be produced at all.

Sources are the BtbN builds (GPL-3.0). That licence covers the FFmpeg binaries
we redistribute; see the licensing note in README before publishing.
"""

import argparse
import os
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

BASE_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download"
TARGET_DIR = Path(__file__).resolve().parent.parent / "ffmpeg"


def target_asset() -> tuple[str, str]:
    """Return (asset_name, member_prefix) for the host platform.

    Asset names use the upstream BtbN convention: linux64 / linuxarm64 /
    win64. There are no macOS builds in that release, so macOS falls back to
    evermeet.cx.
    """
    machine = platform.machine().lower()
    arch = {
        "x86_64": "linux64",
        "amd64": "linux64",
        "arm64": "linuxarm64",
        "aarch64": "linuxarm64",
    }.get(machine)
    if sys.platform == "win32":
        return "ffmpeg-master-latest-win64-gpl.zip", ""
    if sys.platform == "darwin":
        # BtbN publishes no macOS build; evermeet.cx provides a static one.
        return "ffmpeg", ""
    if not arch:
        raise SystemExit(f"Unsupported architecture for bundling: {machine}")
    return f"ffmpeg-master-latest-{arch}-gpl.tar.xz", ""


def download(asset: str, dest: Path) -> Path:
    if sys.platform == "darwin" and asset == "ffmpeg":
        url = "https://evermeet.cx/ffmpeg/getrelease/zip"
    else:
        url = f"{BASE_URL}/{asset}"
    print(f"[ffmpeg] Downloading {url}")
    urllib.request.urlretrieve(url, dest)
    return dest


def extract(asset: str, archive: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(out_dir)
    elif asset.endswith((".tar.xz", ".tar.gz")):
        with tarfile.open(archive) as tf:
            tf.extractall(out_dir)
    else:
        archive.rename(out_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))

    binary_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for candidate in out_dir.rglob(binary_name):
        target = out_dir / binary_name
        if candidate != target:
            candidate.replace(target)
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return target

    raise SystemExit(f"ffmpeg binary not found after extracting {asset}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(TARGET_DIR))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    if (out_dir / binary_name).exists() and not args.force:
        print(f"[ffmpeg] Already present at {out_dir / binary_name}")
        return 0

    asset, _prefix = target_asset()
    archive = out_dir / asset
    try:
        download(asset, archive)
        path = extract(asset, archive, out_dir)
    finally:
        if archive.exists() and archive != out_dir / binary_name:
            try:
                archive.unlink()
            except OSError:
                pass

    print(f"[ffmpeg] Installed {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())