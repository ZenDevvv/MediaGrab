"""Locating the desktop launcher's log file.

The launcher redirects stdio into a log so that a windowed build -- which has no
console -- leaves something a user can hand over when something goes wrong.
This module is the single definition of where that file lives, shared by
``run_desktop.py`` (which writes it) and the settings panel (which reveals and
copies it). Two copies of this path would drift, and a settings panel pointing
at the wrong file is worse than no panel.
"""

import os
import sys
from pathlib import Path

__all__ = ["launcher_log_path", "browser_profile_dir"]


def _app_data_root() -> Path:
    """Per-user writable location for application state.

    Linux deliberately keeps ``~/.mediagrab`` rather than following XDG: the
    path was already in use, and silently moving it would orphan the log of
    anyone running an earlier build -- the one file this exists to surface.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".mediagrab"


def launcher_log_path() -> Path:
    """Path of ``launcher.log`` for the desktop app."""
    return _app_data_root() / "MediaGrab" / "launcher.log"


def browser_profile_dir() -> Path:
    """Profile used by the app-mode browser window.

    A private profile is required, not cosmetic: Chromium and Edge hand a new
    launch off to an already-running instance and exit immediately, which
    previously made the launcher believe the window had closed and tore down a
    backend the open window still needed.
    """
    return _app_data_root() / "MediaGrab" / "browser-profile"