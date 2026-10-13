# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MediaGrab desktop app.

Build with:
    pyinstaller packaging/MediaGrab.spec --noconfirm

Produces a one-directory bundle under dist/. One-directory rather than one-file
because unpacking ~270MB of FFmpeg and Deno on every launch is slow and
fragile, and
antivirus software quarantines self-extracting archives far more aggressively.

Expect FFmpeg to be present at packaging/../ffmpeg (see packaging/fetch_ffmpeg.py).
Without it the app still launches but cannot merge, convert, or embed subtitles.

Expect Deno to be present at packaging/../deno (see packaging/install_deno.py).
Without it the app still launches but cannot solve YouTube's player challenge.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent

block_cipher = None

# yt-dlp pulls in optional extractors dynamically; PyInstaller's static analysis
# misses them, which produces runtime ImportErrors for less common sites.
datas, binaries, hiddenimports = [], [], []
# pythonnet is a Windows-only dependency that pywebview imports dynamically
# through clr_loader, so PyInstaller cannot discover it statically. Left
# uncollected, the frozen Windows build fails at startup with "Failed to
# resolve Python.Runtime.Loader.Initialize from .../pythonnet/runtime/
# Python.Runtime.dll" and silently falls back to a browser window.
for package in ("yt_dlp", "pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "clr",
    "clr_loader",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
    # GUI fallback chain; absent ones degrade at runtime rather than failing to
    # build, so they are imported defensively inside run_desktop.py.
    "webview",
    "httpx",
]

datas += [
    (str(ROOT / "static"), "static"),
]

# GPL-3.0 s4 requires the license text and copyright notice to accompany the
# work. Shipping the binary without LICENSE would make every release
# non-compliant no matter what the repo says, so these are bundled and then
# asserted on by CI.
for legal in ("LICENSE", "NOTICE"):
    path = ROOT / legal
    if path.exists():
        datas.append((str(path), "."))
    else:
        print(f"WARNING: {legal} not found at the repo root; the release will "
              f"be non-compliant with GPL-3.0 section 4.")

ffmpeg_dir = ROOT / "ffmpeg"
if ffmpeg_dir.is_dir():
    # Enumerate the files explicitly rather than passing the directory: this
    # keeps the exec bit intact and makes a missing binary obvious.
    # Verified: dist/MediaGrab/_internal/ffmpeg/ffmpeg is present and runs.
    ffmpeg_files = sorted(p for p in ffmpeg_dir.iterdir() if p.is_file())
    if not ffmpeg_files:
        print("WARNING: ffmpeg/ exists but contains no files; "
              "run packaging/fetch_ffmpeg.py before building.")
    binaries += [(str(p), "ffmpeg") for p in ffmpeg_files]

# Deno is the JS runtime yt-dlp needs for the YouTube player challenge. The
# default YTDLP_JS_RUNTIMES is "deno", so a build shipped without it starts
# fine and then quietly fails to extract from YouTube on the user's machine.
deno_dir = ROOT / "deno"
if deno_dir.is_dir():
    deno_files = sorted(p for p in deno_dir.iterdir() if p.is_file())
    if not deno_files:
        print("WARNING: deno/ exists but contains no files; "
              "run packaging/install_deno.py before building.")
    binaries += [(str(p), "deno") for p in deno_files]
else:
    print("WARNING: deno/ not found; run packaging/install_deno.py before "
          "building or YouTube extraction will be degraded.")

a = Analysis(
    [str(ROOT / "run_desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaGrab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Console=False hides the terminal on Windows; the GUI is the interface.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "static" / "icons" / "favicon.ico")
    if sys.platform == "win32" and (ROOT / "static" / "icons" / "favicon.ico").exists()
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MediaGrab",
)