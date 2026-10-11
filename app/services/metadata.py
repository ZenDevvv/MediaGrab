"""Cached media inspection plus the hosted guardrail checks.

``/api/info`` already runs a full yt-dlp extraction. Re-running it inside
``/api/download`` to enforce the duration and size caps would double probe cost
on every request, so results are memoized for a short window keyed by URL.
The cache is what makes the guardrails authoritative: the server decides from
its own metadata rather than trusting client-supplied duration/size.
"""

import time
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional

from app import config
from app.api.schemas import DownloadRequest, InfoResponse
from app.services.extractor import inspect_url

__all__ = [
    "GuardrailError",
    "estimate_size_bytes",
    "get_cached_metadata",
    "resolve_duration",
    "validate_request",
]


class GuardrailError(ValueError):
    """A hosted-mode limit was exceeded. Carries an HTTP status and safe detail."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class _CacheEntry:
    response: InfoResponse
    stored_at: float


class _MetadataCache:
    """Small TTL + LRU cache around ``inspect_url``.

    Bounded so a hosted VM cannot be driven into unbounded memory growth by a
    stream of distinct URLs.
    """

    def __init__(self, ttl_seconds: int, max_entries: int):
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._entries: Dict[str, _CacheEntry] = {}
        self._lock = Lock()

    def get(self, url: str) -> Optional[InfoResponse]:
        now = time.time()
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return None
            if now - entry.stored_at > self._ttl:
                del self._entries[url]
                return None
            # Refresh recency for LRU eviction.
            self._entries.pop(url, None)
            self._entries[url] = entry
            return entry.response

    def put(self, url: str, response: InfoResponse) -> None:
        with self._lock:
            self._entries[url] = _CacheEntry(response=response, stored_at=time.time())
            while len(self._entries) > self._max_entries:
                oldest_key = next(iter(self._entries))
                del self._entries[oldest_key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _MetadataCache(
    ttl_seconds=config.METADATA_CACHE_TTL_SECONDS,
    max_entries=config.METADATA_CACHE_MAX_ENTRIES,
)


def get_cached_metadata() -> _MetadataCache:
    """Exposed for tests and for invalidation hooks."""
    return _cache


async def _load_metadata(url: str, *, refresh: bool = False) -> InfoResponse:
    if not refresh:
        cached = _cache.get(url)
        if cached is not None:
            return cached
    response = await inspect_url(url)
    _cache.put(url, response)
    return response


def _resolution_to_height(resolution: Optional[str]) -> int:
    if not resolution:
        return 0
    digits = "".join(ch for ch in resolution if ch.isdigit())
    return int(digits) if digits else 0


def resolve_duration(request: DownloadRequest, metadata: InfoResponse) -> int:
    """Duration in seconds that this request will actually produce.

    A trim request only produces the trimmed segment, so the source duration is
    irrelevant to the cap when the client already narrowed the range.
    """
    if request.trim and request.trim.end > request.trim.start:
        return request.trim.end - request.trim.start
    return metadata.duration


def estimate_size_bytes(request: DownloadRequest, metadata: InfoResponse) -> int:
    """Pre-flight size estimate in bytes.

    Uses the per-resolution estimate the extractor already computed. Returns 0
    when nothing is known, which callers must treat as "unbounded" rather than
    "free" -- the in-flight ``--max-filesize`` guard is what backstops that case.
    """
    if request.mode == "srt":
        # Transcript payloads are text; the duration cap is what bounds them.
        return 0

    if request.mode == "mp3":
        audio_kbps = metadata.source_audio_kbps or 128
        target_kbps = request.bitrate or 192
        duration = max(resolve_duration(request, metadata), 0)
        return int(duration * target_kbps * 1000 / 8)

    if request.mode == "mp4":
        wanted = request.quality or "1080p"
        for option in metadata.qualities:
            if option.resolution == wanted and option.approx_bytes > 0:
                return option.approx_bytes
        # Fall back to the largest option that is still within the requested cap.
        wanted_height = _resolution_to_height(wanted)
        candidates: List[int] = [
            opt.approx_bytes for opt in metadata.qualities
            if opt.approx_bytes > 0 and _resolution_to_height(opt.resolution) <= wanted_height
        ]
        if candidates:
            return max(candidates)

    return 0


def validate_request(request: DownloadRequest, metadata: InfoResponse) -> None:
    """Enforce hosted guardrails. Raises :class:`GuardrailError` on violation.

    In desktop mode this is a no-op -- the native app is unlimited by design.
    """
    if not config.IS_HOSTED:
        return

    # Resolution cap. Subtitles have no video stream so they are exempt.
    if request.mode == "mp4":
        wanted_height = _resolution_to_height(request.quality)
        max_height = _resolution_to_height(config.HOSTED_MAX_RESOLUTION)
        if wanted_height > max_height:
            raise GuardrailError(
                f"{request.quality} is limited to {config.HOSTED_MAX_RESOLUTION} on the hosted "
                f"server. Download the Desktop or Android app for 4K and unlimited resolution."
            )

    # Duration cap. Subtitles are exempt: a transcript is text, not media.
    if request.mode != "srt":
        duration = resolve_duration(request, metadata)
        if duration > config.HOSTED_MAX_DURATION_SECONDS:
            trimmed = bool(request.trim and request.trim.end > request.trim.start)
            if trimmed:
                raise GuardrailError(
                    f"The selected trim range is longer than the {config.HOSTED_MAX_DURATION_SECONDS // 60} "
                    f"minute hosted limit. Trim a shorter segment, or download the Desktop or Android app."
                )
            raise GuardrailError(
                f"This video is {duration // 60} minutes long and exceeds the "
                f"{config.HOSTED_MAX_DURATION_SECONDS // 60} minute hosted limit. Trim a segment below, "
                f"or download the Desktop or Android app for full-length videos."
            )

    # Size cap.
    estimated = estimate_size_bytes(request, metadata)
    if estimated > config.HOSTED_MAX_FILE_SIZE_BYTES:
        raise GuardrailError(
            f"This download is estimated at {estimated / (1024 ** 3):.2f} GB, over the "
            f"{config.HOSTED_MAX_FILE_SIZE_BYTES / (1024 ** 3):.0f} GB hosted limit. "
            f"Download the Desktop or Android app for unlimited file size."
        )


async def load_and_validate(request: DownloadRequest) -> Optional[InfoResponse]:
    """Fetch (or reuse) metadata and apply hosted guardrails.

    Returns ``None`` in desktop mode: the native app is unlimited by design and
    should not pay for an extra extraction on the hot path.
    """
    if not config.IS_HOSTED:
        return None

    metadata = await _load_metadata(request.url)
    validate_request(request, metadata)
    return metadata


def quality_options_within(request: DownloadRequest, metadata: InfoResponse) -> List[str]:
    """Resolutions the hosted server will actually accept for this request."""
    allowed = []
    for option in metadata.qualities:
        if config.IS_HOSTED and option.resolution in config.HOSTED_LOCKED_RESOLUTIONS:
            continue
        allowed.append(option.resolution)
    return allowed