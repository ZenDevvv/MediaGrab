# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MediaGrab desktop app.

Build with:
    pyinstaller packaging/MediaGrab.spec --noconfirm

Produces a one-directory bundle under dist/. One-directory rather than one-file
because unpacking ~80MB of FFmpeg on every launch is slow and fragile, and
antivirus software quarantines self-extracting archives far more aggressively.

Expect FFmpeg to be present at packaging/../ffmpeg (see packaging/fetch_ffmpeg.py).
Without it the app still launches but cannot merge, convert, or embed subtitles.
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
for package in ("yt_dlp",):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
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