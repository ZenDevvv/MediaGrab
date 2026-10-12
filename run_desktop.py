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
import traceback
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


def _watch_backend(url: str, server, thread, stop: threading.Event) -> None:
    """Record backend liveness while the window is open.

    Two very different failures both surface as "127.0.0.1 refused to connect"
    in the app window: the backend thread dying, or the webview honouring a
    system proxy that refuses loopback. Only the first is visible from Python,
    so poll and log every alive/dead transition with a timestamp. If the log
    shows the backend stayed up, the window's problem is the proxy, not us.
    """
    import httpx

    alive = None
    while not stop.wait(3.0):
        if not thread.is_alive():
            print("[MediaGrab] watchdog: backend thread has exited")
            return
        try:
            ok = httpx.get(url, timeout=2.0).status_code == 200
        except Exception:
            ok = False
        if ok != alive:
            alive = ok
            stamp = time.strftime("%H:%M:%S")
            print(f"[MediaGrab] {stamp} backend {'reachable' if ok else 'UNREACHABLE'}")


def _report_fatal(log_path: Optional[Path], message: str) -> None:
    """Explain a startup failure in a window, not just the log.

    A desktop app that fails should say so. Without this the only symptom is
    an app window showing the browser's connection error, which tells the user
    nothing about MediaGrab at all.
    """
    print(f"[MediaGrab] {message}")
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "MediaGrab", 0x10)
            return
        except Exception:
            pass
    try:
        import webbrowser

        webbrowser.open("about:blank")
    except Exception:
        pass


def launcher_log_path() -> Path:
    """Where the launcher writes its log.

    A windowed build has no console, so anything written to stdout/stderr is
    invisible to the user. Worse, an exception raised inside the server thread
    is printed by the threading machinery to stderr and then lost, which is
    how a dead backend could present as nothing more than a browser error page
    inside the app window.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        root = Path.home() / ".mediagrab"
    return root / "MediaGrab" / "launcher.log"


def _open_launcher_log():
    """Open the launcher log for appending, or fall back to the null device."""
    try:
        path = launcher_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None, open(os.devnull, "w", encoding="utf-8")


def _ensure_standard_streams() -> Optional[Path]:
    """Give the process real stdio even in a windowed (no-console) build.

    PyInstaller builds a GUI-subsystem executable on Windows because the spec
    sets ``console=False``, and that bootloader sets ``sys.stdout``,
    ``sys.stderr`` and ``sys.stdin`` to ``None``. The failure this causes is
    quiet rather than obvious: ``print()`` against a ``None`` stream is a
    silent no-op, so the launcher's own status lines vanish without complaint,
    and the app then dies somewhere unrelated -- uvicorn's ``DefaultFormatter``
    calls ``sys.stdout.isatty()`` while building its logging config, which
    raises ``AttributeError: 'NoneType' object has no attribute 'isatty'``
    before the server ever binds.

    Binding them to the launcher log restores objects that are writable and
    ``isatty()``-able. ``isatty()`` then reports ``False``, which is the
    correct answer for a GUI app: uvicorn disables ANSI colouring rather than
    emitting escape codes nobody will see. Streams that already exist are
    never replaced, so running from a terminal still shows output there.

    Returns the log path when a file was opened, or ``None`` if the null
    device had to be used.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None

    path, stream = _open_launcher_log()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")

    if path:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[MediaGrab] {stamp} launcher started (no console; logging to {path})")
    return path


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
            # CSIDL_DOWNLOADS is 0x05. 0x28 is CSIDL_PROFILE, which resolves
            # the user profile root -- that bug put downloads in C:\Users\<name>
            # instead of C:\Users\<name>\Downloads.
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x05, None, 0, path)
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

    def _run_server() -> None:
        # An exception here is raised on a background thread, so the threading
        # machinery prints it to stderr and the thread simply dies. In a
        # windowed build that output used to go nowhere, which turned a dead
        # backend into a bare "127.0.0.1 refused to connect" page inside the
        # app window with no explanation anywhere. Log it explicitly.
        try:
            server.run()
        except BaseException:
            traceback.print_exc()
            print("[MediaGrab] FATAL: backend thread exited; the window will show a connection error")
            server.startup_failed = True

    thread = threading.Thread(target=_run_server, daemon=True)
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

    # Windows: browsers installed normally are not on PATH, so probe the
    # standard locations. Edge is preinstalled on Windows 11 and auto-installed
    # on Windows 10, which makes it the reliable last-resort GUI on a clean
    # machine. Without this a non-technical user with no Chromium hits the
    # external-browser fallback and loses the app window.
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            if not base:
                continue
            for rel in (
                r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ):
                candidate = Path(base) / rel
                if candidate.exists():
                    return str(candidate)

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
            # A dedicated profile directory is required, not cosmetic. Chromium
            # and Edge hand a new launch off to an already-running instance and
            # exit immediately; if that happens here, proc.wait() returns while
            # the window is still open, the caller tears the backend down, and
            # the live window reports "127.0.0.1 refused to connect". A private
            # profile forces a separate process that owns the window.
            profile = launcher_log_path().parent / "browser-profile"
            profile.mkdir(parents=True, exist_ok=True)
            print(f"[MediaGrab] Opening app-mode window via {chromium}...")
            proc = subprocess.Popen(
                [
                    chromium,
                    f"--app={url}",
                    "--window-size=1200,860",
                    f"--user-data-dir={profile}",
                    # Skip first-run and default-browser prompts, which can
                    # otherwise swallow the window and exit.
                    "--no-first-run",
                    "--no-default-browser-check",
                    # The URL is always loopback. Without these, a machine with
                    # a system proxy configured (common in VMs and corporate
                    # images) routes 127.0.0.1 through that proxy, which then
                    # refuses it -- the app window opens on a live backend and
                    # still shows "127.0.0.1 refused to connect".
                    "--no-proxy-server",
                    "--proxy-bypass-list=127.0.0.1;localhost",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Detect hand-off: the process exits within seconds while a window
            # may still be open elsewhere. Serving must not stop in that case.
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.25)
            if proc.poll() is not None:
                print(
                    "[MediaGrab] Browser handed off to a running instance; "
                    "keeping the backend alive so the window keeps working"
                )
                while True:
                    time.sleep(1)

            # The window owns this process: wait until the user closes it. A
            # fixed timeout used to tear the backend down after two minutes
            # while the window was still open and downloading.
            proc.wait()
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

    # Before start_server(), which imports uvicorn and configures logging.
    log_path = _ensure_standard_streams()

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

    # Opening a window at a dead backend is the worst possible outcome: the
    # user sees "127.0.0.1 refused to connect" and has no idea the app itself
    # failed. Verify once more immediately before handing over to the GUI --
    # the backend can die between startup and here -- and explain the failure
    # instead of navigating to it.
    try:
        import httpx

        if httpx.get(url, timeout=5.0).status_code != 200:
            raise RuntimeError("backend stopped responding before the window opened")
        print("[MediaGrab] Backend confirmed reachable")
    except Exception as e:
        print(f"[MediaGrab] FATAL: backend unreachable before window: {e}")
        _report_fatal(log_path, f"MediaGrab could not start its local server.\n\n{e}\n\nDetails: {log_path}")
        server.should_exit = True
        return 1

    stop = threading.Event()
    watchdog = threading.Thread(
        target=_watch_backend, args=(url, server, thread, stop), daemon=True
    )
    watchdog.start()

    try:
        return launch(url, prefer_native=not args.no_native)
    finally:
        stop.set()
        print("[MediaGrab] Window closed; shutting down backend")
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())