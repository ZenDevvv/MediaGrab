"""HTML template rendering.

Serves the mode as a meta tag injected at request time. The frontend reads mode
from here rather than inferring it from an async capabilities fetch, which makes
mode resolution race-free and survives a capabilities endpoint that 404s against
a stale backend (§1.2 Decision #9).
"""

import re
from typing import Optional

__all__ = ["render_html", "META_MODE_PATTERN"]

META_MODE_PATTERN = re.compile(
    r'<meta\s+name="mg-mode"\s+content="[^"]*"\s*/?>',
    re.IGNORECASE,
)

_HEAD_PLACEHOLDER = "<!--mg-mode-->"

_HEAD_INJECTION = (
    '  <meta name="mg-mode" content="{mode}">\n'
    '  <meta name="mg-retention-seconds" content="{retention}">\n'
)

_HOSTED_SAFE_TEMPLATE = """  <meta name="mg-mode" content="hosted">
  <meta name="mg-retention-seconds" content="60">
"""


def render_html(html: str, mode: str, retention_seconds: int) -> str:
    """Return ``html`` with mode metadata injected.

    The static files ship a ``<!--mg-mode-->`` placeholder so the injection point
    exists in the source of truth. If that placeholder has been stripped (an
    older file, or an edited template) we fall back to injecting before
    ``</head>`` rather than silently serving a page with no mode at all.
    """
    injected = _HEAD_INJECTION.format(mode=mode, retention=int(retention_seconds))

    if _HEAD_PLACEHOLDER in html:
        return html.replace(_HEAD_PLACEHOLDER, injected.rstrip("\n"))

    if "<head>" in html:
        return html.replace("<head>", "<head>\n" + injected.rstrip("\n"), 1)

    # Last resort: the template has no head. Mode would be unavailable and the
    # frontend degrades to hosted-safe defaults, which is the safe failure.
    return html