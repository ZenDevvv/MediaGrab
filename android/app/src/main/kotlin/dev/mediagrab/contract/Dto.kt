package dev.mediagrab.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Wire types for the MediaGrab `/api/` contract.
 *
 * These are a transcription of `openapi-contract.json` — the snapshot of the
 * OpenAPI document FastAPI serves at `/openapi.json`. That file is the source
 * of truth; `tests/test_api_contract.py` asserts it still matches the Python
 * app, and `ApiContractTest` here asserts these classes still match it.
 *
 * **Every @SerialName is load-bearing.** Pydantic emits snake_case
 * (`job_id`, `download_url`, `queue_position`). Kotlin's default property
 * naming is camelCase, so without the annotations below the server would emit
 * `{"jobId": …}`, the shared frontend would read `.job_id`, receive
 * `undefined`, and render nothing — with no exception anywhere to explain it.
 * The failure is silent, Android-only, and looks like a UI bug.
 *
 * Naming note: `mp4Mode` rather than `mp4` because the property would
 * otherwise start with a digit.
 */

@Serializable
data class TrimConfig(
    val start: Int = 0,
    val end: Int = 0,
)

@Serializable
data class SubtitlesConfig(
    val lang: String,
    val embed: Boolean = true,
)

@Serializable
data class DownloadRequest(
    val url: String,
    @SerialName("mode") val mp4Mode: String = "mp4",
    val quality: String? = "1080p",
    val bitrate: Int? = 192,
    val trim: TrimConfig? = null,
    val subtitles: SubtitlesConfig? = null,
)

@Serializable
data class DownloadResponse(
    @SerialName("job_id") val jobId: String,
)

@Serializable
data class QualityOption(
    val resolution: String,
    @SerialName("approx_bytes") val approxBytes: Long = 0,
    val type: String = "remux",
)

@Serializable
data class SubtitleOption(
    val lang: String,
    val label: String,
    @SerialName("is_auto") val isAuto: Boolean = false,
)

@Serializable
data class InfoResponse(
    val url: String,
    val title: String,
    val uploader: String,
    val duration: Int,
    val thumbnail: String? = null,
    @SerialName("source_audio_kbps") val sourceAudioKbps: Int = 128,
    @SerialName("source_audio_codec") val sourceAudioCodec: String = "opus",
    val qualities: List<QualityOption> = emptyList(),
    val subtitles: List<SubtitleOption> = emptyList(),
)

@Serializable
data class LimitsResponse(
    @SerialName("max_resolution") val maxResolution: String? = null,
    @SerialName("max_duration_seconds") val maxDurationSeconds: Int? = null,
    @SerialName("max_file_size_bytes") val maxFileSizeBytes: Long? = null,
    @SerialName("concurrency_limit") val concurrencyLimit: Int? = null,
    @SerialName("per_ip_limit") val perIpLimit: Int? = null,
    @SerialName("per_session_limit") val perSessionLimit: Int? = null,
    @SerialName("idle_watchdog_seconds") val idleWatchdogSeconds: Int? = null,
    @SerialName("locked_resolutions") val lockedResolutions: List<String> = emptyList(),
)

@Serializable
data class DownloadsResponse(
    @SerialName("windows_url") val windowsUrl: String? = null,
    @SerialName("linux_url") val linuxUrl: String? = null,
    @SerialName("android_url") val androidUrl: String? = null,
)

@Serializable
data class JsRuntimeResponse(
    val configured: List<String> = emptyList(),
    val available: List<String> = emptyList(),
    val missing: List<String> = emptyList(),
)

@Serializable
data class CapabilitiesResponse(
    /**
     * Always "desktop".
     *
     * The app is single-user and on-device: there is no shared pool to ration,
     * no proxy to distrust and no retention policy because downloaded files
     * belong to the user and live in their own Downloads folder. The frontend
     * branches on this value, so it must be a mode it knows — see
     * ARCHITECTURE.md §5 for what each mode changes in the UI.
     */
    val mode: String,
    val version: String = "dev",
    @SerialName("retention_seconds") val retentionSeconds: Int,
    val limits: LimitsResponse,
    val downloads: DownloadsResponse,
    @SerialName("js_runtimes") val jsRuntimes: JsRuntimeResponse,
)

@Serializable
data class JobProgressResponse(
    @SerialName("job_id") val jobId: String,
    val status: String,
    val progress: Double = 0.0,
    val speed: String? = null,
    val eta: String? = null,
    val got: String? = null,
    val stage: String? = null,
    val filename: String? = null,
    val size: String? = null,
    @SerialName("download_url") val downloadUrl: String? = null,
    @SerialName("srt_filename") val srtFilename: String? = null,
    @SerialName("srt_url") val srtUrl: String? = null,
    val error: String? = null,
    val code: String? = null,
    @SerialName("queue_position") val queuePosition: Int? = 0,
    @SerialName("active_slots") val activeSlots: Int = 0,
)

/** Mirrors FastAPI's `HTTPException.detail`, which may be a string or an object. */
@Serializable
data class ErrorDetail(
    val message: String,
    val code: String? = null,
    val remedy: String? = null,
    @SerialName("retry_after_seconds") val retryAfterSeconds: Int? = null,
)