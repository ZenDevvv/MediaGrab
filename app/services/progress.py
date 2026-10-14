"""Where each phase of a job sits on the progress bar.

The bar has to move steadily from the moment a job starts to the moment the
file is ready. Only downloading has a real measure (bytes), so it owns the
bulk of the bar; the phases around it get fixed slices and the page eases
through them while it waits for the next report:

    0 ..  4   reading the link          (no measure; the page creeps)
    4 .. 88   downloading               (real bytes)
   88 .. 96   merging, converting, tags (stage floors; the page creeps)
   96 .. 100  saving into Downloads

android/.../engine/ProgressTracker.kt mirrors these numbers; keep them in step.
"""

READ_END = 4.0
DOWNLOAD_END = 88.0
PROCESS_START = 90.0
TAGS_AT = 94.0
PROCESS_END = 96.0
SAVE_END = 99.0

# With no sizes to go on, a video+audio download is mostly video.
VIDEO_SHARE = 0.85


def download_pct(fraction: float) -> float:
    """Map 0..1 of the download phase onto the bar."""
    fraction = min(1.0, max(0.0, fraction))
    return round(READ_END + fraction * (DOWNLOAD_END - READ_END), 1)
