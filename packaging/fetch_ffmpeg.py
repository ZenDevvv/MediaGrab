#!/usr/bin/env python3
"""Fetch a static FFmpeg build for the current platform.

Bundled so the desktop app works out of the box on a machine with no FFmpeg
installed. FFmpeg is required for merging, MP3 conversion, subtitle embedding
and trimming -- without it most downloads cannot be produced at all.

Sources are the BtbN builds (GPL-3.0). That licence covers the FFmpeg binaries
we redistribute; see the licensing note in README before publishing.
"""

import argparse
import hashlib
import json
import platform
import re
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download"
RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
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


def write_provenance(out_dir: Path) -> Path:
    """Record which FFmpeg source this binary corresponds to.

    GPL-3.0 s4 obliges the distributor to offer the complete corresponding
    source for the FFmpeg it ships. BtbN publishes that source per release, but
    `releases/latest` is a moving tag: the asset name that resolves today
    resolves to a different build tomorrow, and the record of what a given
    binary actually was would be gone.

    So pin what can be pinned. The FFmpeg commit is read out of the binary via
    `ffmpeg -version`, so it describes what is actually shipped rather than
    what the build script believed at the time. Nothing here is a manual step,
    and nothing here is allowed to fail a build.
    """
    binary_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    binary = out_dir / binary_name
    asset, _prefix = target_asset()

    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    resolved_tag = "unknown"
    try:
        with urllib.request.urlopen(RELEASE_API, timeout=30) as response:
            resolved_tag = json.load(response).get("tag_name", "unknown")
    except Exception as exc:  # provenance must never block a build
        resolved_tag = f"unresolved ({exc})"

    # `ffmpeg -version` prints the exact upstream FFmpeg commit it was built
    # from, in the form "N-127507-g24e860f6d3-20261013". That is a far
    # stronger answer to "which source corresponds to this binary" than the
    # BtbN release tag, which is a moving tag that resolves to something else
    # tomorrow. Record both the raw build string and the bare git hash, which
    # is the part that forms a valid commit URL.
    build = "unknown"
    commit = ""
    try:
        result = subprocess.run(
            [str(binary), "-version"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout:
            build = result.stdout.splitlines()[0].split()[2]
            match = re.search(r"-g([0-9a-f]{7,40})", build)
            if match:
                commit = match.group(1)
    except Exception as exc:
        build = f"unresolved ({exc})"

    upstream_url = (
        f"https://github.com/FFmpeg/FFmpeg/commit/{commit}"
        if commit
        else "https://github.com/FFmpeg/FFmpeg"
    )

    provenance = out_dir / "PROVENANCE.txt"
    provenance.write_text(
        "FFmpeg binary bundled with MediaGrab\n"
        "==================================\n\n"
        f"ffmpeg build   {build}\n"
        f"ffmpeg commit  {commit or 'unresolved'}\n"
        f"asset          {asset}\n"
        f"sha256         {digest}\n"
        f"release tag    {resolved_tag}  (as resolved {datetime.now(timezone.utc).date()})\n"
        f"binary source  {BASE_URL}/{asset}\n"
        f"source code    https://github.com/BtbN/FFmpeg-Builds/releases/tag/{resolved_tag}\n"
        f"upstream       {upstream_url}\n"
        f"licence        GPL-3.0-or-later (built with --enable-gpl)\n\n"
        "'ffmpeg commit' is the FFmpeg commit this binary was built from, and\n"
        "is the corresponding source for GPL-3.0 section 4. It is read from\n"
        "the binary itself, so it cannot drift from what is shipped.\n\n"
        "MediaGrab is distributed under GPL-3.0-or-later. FFmpeg is invoked as\n"
        "a separate executable process and is not linked into MediaGrab.\n"
        "See NOTICE in the MediaGrab repository.\n",
        encoding="utf-8",
    )
    return provenance


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
        # Still write provenance. Skipping the download is a reason not to
        # re-fetch, not a reason to skip the GPL-3.0 s4 obligation -- and this
        # is the path taken by any developer who ran the fetcher before
        # provenance existed, which is how a release ends up shipping a binary
        # whose corresponding source cannot be identified.
        if (out_dir / "PROVENANCE.txt").exists():
            print("[ffmpeg] Provenance already recorded")
        else:
            print(f"[ffmpeg] Provenance written to {write_provenance(out_dir)}")
        return 0

    asset, _prefix = target_asset()
    archive = out_dir / asset
    try:
        download(asset, archive)
        path = extract(asset, archive, out_dir)
        provenance = write_provenance(out_dir)
    finally:
        if archive.exists() and archive != out_dir / binary_name:
            try:
                archive.unlink()
            except OSError:
                pass

    print(f"[ffmpeg] Installed {path}")
    print(f"[ffmpeg] Provenance written to {provenance}")
    return 0


if __name__ == "__main__":
    sys.exit(main())