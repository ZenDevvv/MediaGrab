"""Resolve which JavaScript runtimes yt-dlp can actually use.

yt-dlp needs a JS runtime to solve YouTube's player challenge, and the
configured list is a *policy*, not a measurement. Naming a runtime that is not
installed does not fail loudly -- yt-dlp logs "no JavaScript runtime could be
found" and carries on with a weakened extraction path -- so a misconfigured
deployment looks healthy right up until YouTube starts refusing links.

So the policy is intersected with what is actually on PATH, once per process
(the PATH does not change under a running server) and exposed for the
capabilities endpoint to report.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from typing import List

from app import config

__all__ = ["available_runtimes", "runtime_status", "missing_runtimes"]


@lru_cache(maxsize=1)
def available_runtimes() -> tuple:
    """Runtimes that are configured *and* installed, in configured order.

    An empty tuple is a valid and common answer: nothing here degrades the app
    to "no runtimes", it just means the caller decides whether to warn.
    """
    found: List[str] = []
    for name in config.YTDLP_JS_RUNTIMES:
        name = (name or "").strip()
        if name and name not in found and shutil.which(name):
            found.append(name)
    return tuple(found)


def runtime_status() -> dict:
    """A JSON-safe snapshot for /api/capabilities."""
    configured = [n.strip() for n in config.YTDLP_JS_RUNTIMES if n.strip()]
    available = list(available_runtimes())
    return {
        "configured": configured,
        "available": available,
        "missing": [n for n in configured if n not in available],
    }


def missing_runtimes() -> List[str]:
    return runtime_status()["missing"]