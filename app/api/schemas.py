from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class InfoRequest(BaseModel):
    url: str = Field(..., description="Target media URL")

class QualityOption(BaseModel):
    resolution: str
    approx_bytes: int = 0
    type: Literal["remux", "encode"] = "remux"

class SubtitleOption(BaseModel):
    lang: str
    label: str
    is_auto: bool = False

class InfoResponse(BaseModel):
    url: str
    title: str
    uploader: str
    duration: int
    thumbnail: Optional[str] = None
    source_audio_kbps: int = 128
    source_audio_codec: str = "opus"
    qualities: List[QualityOption] = Field(default_factory=list)
    subtitles: List[SubtitleOption] = Field(default_factory=list)

class TrimConfig(BaseModel):
    start: int = 0
    end: int = 0

class SubtitlesConfig(BaseModel):
    lang: str
    embed: bool = True

class DownloadRequest(BaseModel):
    url: str
    mode: Literal["mp4", "mp3", "srt"] = "mp4"
    quality: Optional[str] = "1080p"
    bitrate: Optional[int] = 192
    trim: Optional[TrimConfig] = None
    subtitles: Optional[SubtitlesConfig] = None

class DownloadResponse(BaseModel):
    job_id: str

class JobProgressResponse(BaseModel):
    job_id: str
    status: Literal["queued", "extracting", "downloading", "processing", "ready", "error"]
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    got: Optional[str] = None
    stage: Optional[str] = None
    filename: Optional[str] = None
    size: Optional[str] = None
    download_url: Optional[str] = None
    srt_filename: Optional[str] = None
    srt_url: Optional[str] = None
    error: Optional[str] = None
