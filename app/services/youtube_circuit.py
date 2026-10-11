"""YouTube request circuit breaker.

The hosted instance runs from a datacenter address that YouTube refuses to serve
anonymously for most videos. Every available client, JS runtime, impersonation
setting and yt-dlp build behaves identically, so the cause is address
reputation.

The naive fix -- detect YouTube up front and refuse -- is wrong twice: it denies
the minority of links that do still work, and it turns the hosted instance into
a service that is transparently not trying. The naive alternative -- keep
trying -- is worse, because each failed attempt is another request from an
address YouTube is already suspicious of, and repeated failures are what turn a
challenge into a block.

So: probe at most once per cooldown window (§1.5 Decision #17). The first bot
check opens the circuit; while open no request reaches YouTube and callers get
the upsell. When the window expires one probe is allowed through, and a success
closes the circuit permanently for the process.

State is per-process and intentionally not persisted: the hosted instance is a
single process, and losing the circuit on restart is the safe direction.
"""

import time
from typing import Optional

from app import config

__all__ = ["YouTubeCircuit", "circuit", "is_youtube_url"]


def is_youtube_url(url: str) -> bool:
    """True for hosts YouTube serves media from.

    Deliberately narrow: youtu.be short links count, but lookalike hosts such as
    ``notyoutube.com`` must not be mistaken for the real thing.
    """
    if not url:
        return False
    lowered = url.strip().lower()
    for prefix in ("http://", "https://"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    host = lowered.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # Strip credentials and port.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


class YouTubeCircuit:
    """Cooldown gate for outbound YouTube requests."""

    def __init__(self, cooldown_seconds: int):
        self._cooldown = max(0, cooldown_seconds)
        self._open_until: float = 0.0
        self._probe_in_flight = False
        self._trip_count = 0

    def reset(self, cooldown_seconds: Optional[int] = None) -> None:
        if cooldown_seconds is not None:
            self._cooldown = max(0, cooldown_seconds)
        self._open_until = 0.0
        self._probe_in_flight = False
        self._trip_count = 0

    @property
    def trip_count(self) -> int:
        return self._trip_count

    def remaining(self, now: Optional[float] = None) -> float:
        """Seconds until the circuit closes. Zero when closed."""
        now = now if now is not None else time.monotonic()
        return max(0.0, self._open_until - now)

    def is_open(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) > 0.0

    def allows_request(self, url: str, now: Optional[float] = None) -> bool:
        """Whether a request for ``url`` may be attempted right now."""
        if not is_youtube_url(url):
            return True
        if self.is_open(now):
            return False
        # Serialise probes: while one is in flight, others wait rather than
        # stampeding YouTube with the single allowed attempt.
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self, now: Optional[float] = None) -> None:
        """The probe worked. Close the circuit and allow normal traffic."""
        self._probe_in_flight = False
        self._open_until = 0.0

    def record_failure(self, now: Optional[float] = None) -> None:
        """The probe hit the bot check. Open the circuit for the cooldown."""
        now = now if now is not None else time.monotonic()
        self._probe_in_flight = False
        self._trip_count += 1
        self._open_until = now + self._cooldown

    def release_probe(self) -> None:
        """Return an unused probe slot (e.g. the attempt raised unexpectedly)."""
        self._probe_in_flight = False


circuit = YouTubeCircuit(config.YOUTUBE_COOLDOWN_SECONDS)