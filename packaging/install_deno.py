#!/usr/bin/env python3
"""Fetch a static Deno build for the current platform.

yt-dlp cannot solve YouTube's player challenge without a JavaScript runtime,
and Deno is the runtime the project enables by default. Nothing about that is
optional: `YTDLP_JS_RUNTIMES` defaults to "deno", so on a machine where Deno
is not installed the app quietly degrades to a warning and a much lower YouTube
success rate.

This mirrors packaging/fetch_ffmpeg.py so the two bundled binaries are fetched
by the same mechanism, into sibling directories (`ffmpeg/` and `deno/`).

Deno is published under Apache-2.0 with an LLVM exception, so bundling it does
not carry FFmpeg's GPL-3.0 obligation.

Usage:
    python packaging/install_deno.py
    python packaging/install_deno.py --out /usr/local/share/deno
"""

import argparse
import platform
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

RELEASE_URL = "https://github.com/denoland/deno/releases/latest/download"
TARGET_DIR = Path(__file__).resolve().parent.parent / "deno"


def target_asset() -> str:
    """Return the release asset name for the host platform.

    Deno's naming is <machine>-<vendor>-<os>-<abi>, which differs from
    FFmpeg's, so this is a lookup rather than a format string.
    """
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        if machine in ("amd64", "x86_64"):
            return "deno-x86_64-pc-windows-msvc.zip"
        if machine in ("arm64", "aarch64"):
            return "deno-aarch64-pc-windows-msvc.zip"
    elif system == "Linux":
        if machine in ("amd64", "x86_64"):
            return "deno-x86_64-unknown-linux-gnu.zip"
        if machine in ("arm64", "aarch64"):
            return "deno-aarch64-unknown-linux-gnu.zip"
        if machine in ("armv7l", "armv7"):
            return "deno-armv7-unknown-linux-gnueabihf.zip"
    elif system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "deno-aarch64-apple-darwin.zip"
        if machine in ("amd64", "x86_64"):
            return "deno-x86_64-apple-darwin.zip"

    raise SystemExit(f"No Deno build available for {system}/{machine}")


def extract(asset: str, archive: Path, out_dir: Path) -> Path:
    binary_name = "deno.exe" if sys.platform == "win32" else "deno"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(out_dir)

    target = out_dir / binary_name
    if not target.exists():
        raise SystemExit(f"{binary_name} not found inside {asset}")

    # ZIP extraction does not preserve the exec bit, and a non-executable deno
    # fails in a way that looks like "no runtime found" rather than "permission
    # denied" -- so set it explicitly.
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(TARGET_DIR))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "deno.exe" if sys.platform == "win32" else "deno"
    binary = out_dir / binary_name

    if binary.exists() and not args.force:
        print(f"[deno] Already present at {binary}")
        return 0

    asset = target_asset()
    url = f"{RELEASE_URL}/{asset}"

    # Download to a temp file outside the bundle directory: an interrupted
    # download would otherwise leave a truncated zip that the next run treats
    # as "already installed" if it happens to be named deno.
    print(f"[deno] Downloading {url}")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(url) as response, tmp_path.open("wb") as fh:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        path = extract(asset, tmp_path, out_dir)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    print(f"[deno] Installed {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())