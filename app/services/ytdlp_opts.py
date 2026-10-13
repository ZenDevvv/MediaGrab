"""Shared yt-dlp option construction.

Client selection, cookies and JS runtime policy are identical for metadata
extraction and downloading, so they live in one place. Divergence between the
two is how you end up with an inspect that succeeds and a download that fails.
"""

from typing import Any, Dict, Optional

from app import config
from app.services import js_runtime

__all__ = ["base_ytdl_opts", "player_clients"]


def player_clients() -> list:
    clients = [c.strip() for c in config.YTDLP_PLAYER_CLIENTS if c.strip()]
    return clients or ["default"]


def base_ytdl_opts(
    *,
    quiet: bool = True,
    skip_download: bool = False,
    progress_hooks=None,
    postprocessor_hooks=None,
    outtmpl: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Options shared by the extractor and the downloader."""
    opts: Dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": skip_download,
        "extractor_args": {"youtube": {"player_client": player_clients()}},
    }

    # Without a JS runtime yt-dlp cannot solve the player challenge and warns
    # that extraction is deprecated; on some videos it is the difference
    # between formats and an error.
    #
    # Only runtimes that are actually installed are declared. Naming an absent
    # one costs nothing but a warning in yt-dlp's own log, which nothing here
    # surfaces -- it just silently lowers YouTube success. Filtering here makes
    # the absence visible instead, via /api/capabilities.
    runtimes = list(js_runtime.available_runtimes())
    if runtimes:
        opts["js_runtimes"] = {name: {} for name in runtimes}

    if config.YTDLP_COOKIES_PATH:
        opts["cookiefile"] = config.YTDLP_COOKIES_PATH

    # Egress proxy. The root fix for YouTube's datacenter-IP bot check.
    if config.YTDLP_PROXY:
        opts["proxy"] = config.YTDLP_PROXY

    # TLS impersonation. Measured worse on a datacenter IP, so opt-in only.
    if config.YTDLP_IMPERSONATE:
        opts["impersonate"] = config.YTDLP_IMPERSONATE

    if outtmpl:
        opts["outtmpl"] = outtmpl
    if progress_hooks:
        opts["progress_hooks"] = progress_hooks
    if postprocessor_hooks:
        opts["postprocessor_hooks"] = postprocessor_hooks
    if extra:
        opts.update(extra)

    return opts