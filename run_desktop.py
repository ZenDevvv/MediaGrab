#!/usr/bin/env python3
"""MediaGrab desktop launcher.

Starts the backend on an ephemeral loopback port in desktop mode (unlimited,
no retention), then opens a native window over it. Falls back through
pywebview -> Chrome/Chromium --app -> the default browser, because a missing
GTK/WebKit stack must not stop the app from running.

Downloads are written directly to the user's Downloads folder; see
§1.3 Decisions #12-#14 in MULTI_PLATFORM_IMPLEMENTATION_PLAN.md.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "_internal"  # PyInstaller onedir layout
APP_ROOT = DIST_DIR if (DIST_DIR / "static").is_dir() else BASE_DIR

# Must be set before app.config is imported.
os.environ.setdefault("MEDIAGRAB_MODE", "desktop")
os.environ.setdefault("RETENTION_ENABLED", "false")
os.environ.setdefault("USER_FACING_FILENAMES", "true")


def default_downloads_dir() -> Path:
    """The OS Downloads folder, with a safe fallback if it cannot be resolved."""
    try:
        from app.config import DOWNLOADS_DIR  # honours MEDIA_GRAB_DOWNLOADS_DIR
        override = os.getenv("MEDIA_GRAB_DOWNLOADS_DIR")
        if override:
            return Path(override)
    except Exception:
        pass

    home = Path.home()
    if sys.platform == "darwin":
        return home / "Downloads"
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            buf = wintypes.MAX_PATH
            path = ctypes.create_unicode_buffer(buf)
            # CSIDL_PROFILE 0x28 -> FOLDERID_Downloads
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x28, None, 0, path)
            if path.value:
                return Path(path.value)
        except Exception:
            pass
        return home / "Downloads"

    # Linux/XDG
    xdg = os.getenv("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    config_home = Path(os.getenv("XDG_CONFIG_HOME") or (home / ".config"))
    dirs_file = config_home / "user-dirs.dirs"
    if dirs_file.exists():
        try:
            for line in dirs_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("XDG_DOWNLOAD_DIR"):
                    raw = line.split("=", 1)[1].strip().strip('"')
                    return Path(raw.replace("$HOME", str(home)))
        except Exception:
            pass
    return home / "Downloads"


def configure_downloads_dir() -> Path:
    target = default_downloads_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[MediaGrab] Could not use {target}: {e}")
        target = APP_ROOT / "downloads"
        target.mkdir(parents=True, exist_ok=True)
    os.environ["MEDIA_GRAB_DOWNLOADS_DIR"] = str(target)
    return target


def prepend_bundled_ffmpeg() -> Optional[Path]:
    """Put a bundled FFmpeg on PATH so postprocessing works without setup."""
    candidates = [
        APP_ROOT / "ffmpeg",
        APP_ROOT / "_internal" / "ffmpeg",
        BASE_DIR / "ffmpeg",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            return candidate
    return None


def start_server(port: int = 0) -> tuple:
    """Run uvicorn on loopback and return (server, thread, port).

    A port of 0 binds an ephemeral port, which avoids colliding with anything
    already listening on the usual ports.
    """
    import uvicorn

    from app.main import app

    if not port:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    import httpx

    url = f"http://127.0.0.1:{port}"
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return server, thread, port
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Backend did not become ready in time.")


def find_chromium() -> Optional[str]:
    for name in (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "chrome", "microsoft-edge", "msedge",
    ):
        found = shutil.which(name)
        if found:
            return found
    # macOS app bundles
    if sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(path).exists():
                return path
    return None


def launch(url: str, prefer_native: bool = True, timeout: int = 120) -> int:
    """Open the app window, degrading through the fallback chain."""
    if prefer_native:
        try:
            import webview  # type: ignore

            print("[MediaGrab] Opening native window...")
            webview.create_window("MediaGrab", url, width=1200, height=860)
            webview.start()
            return 0
        except Exception as e:
            print(f"[MediaGrab] Native window unavailable ({e}); falling back.")

    chromium = find_chromium()
    if chromium:
        try:
            print(f"[MediaGrab] Opening app-mode window via {chromium}...")
            proc = subprocess.Popen(
                [chromium, f"--app={url}", "--window-size=1200,860"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # The window lives as long as the user keeps it open; we block here
            # so the Python process stays alive as its owner.
            proc.wait(timeout=timeout)
            return 0
        except subprocess.TimeoutExpired:
            print("[MediaGrab] Window closed after timeout.")
            return 0
        except Exception as e:
            print(f"[MediaGrab] App-mode window failed ({e}); falling back.")

    print("[MediaGrab] Opening in the default browser...")
    webbrowser.open(url)
    # No window to wait on; keep serving until interrupted.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MediaGrab desktop application")
    parser.add_argument("--no-browser", action="store_true",
                        help="Start the backend only (for smoke tests).")
    parser.add_argument("--no-native", action="store_true",
                        help="Skip pywebview and go straight to app-mode.")
    parser.add_argument("--port", type=int, default=0,
                        help="Bind a fixed port instead of an ephemeral one.")
    args = parser.parse_args()

    sys.path.insert(0, str(APP_ROOT))

    downloads = configure_downloads_dir()
    ffmpeg_dir = prepend_bundled_ffmpeg()

    print(f"[MediaGrab] Downloads: {downloads}")
    print(f"[MediaGrab] FFmpeg:   {ffmpeg_dir or 'system PATH (postprocessing may fail)'}")

    server, thread, port = start_server(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"[MediaGrab] Backend ready at {url}")

    if args.no_browser:
        print("[MediaGrab] --no-browser set; backend running. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return 0

    try:
        return launch(url, prefer_native=not args.no_native)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())