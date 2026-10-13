package dev.mediagrab.engine

import dev.mediagrab.contract.DownloadRequest
import dev.mediagrab.contract.InfoResponse
import dev.mediagrab.contract.JobProgressResponse

/**
 * The media engine behind the HTTP surface.
 *
 * Splitting this out is what makes the server testable. `YoutubeDlEngine`
 * wraps `youtubedl-android` and needs a real device; `ApiServer` is pure
 * Ktor and does not. Every HTTP behaviour -- status codes, SSE framing,
 * streaming, error shapes -- can therefore be tested on the JVM with a fake
 * engine, with no emulator in the loop.
 *
 * This mirrors the Python split between `app/api/routes.py` (HTTP) and
 * `app/services/extractor.py` + `downloader.py` (yt-dlp), which exists for
 * the same reason.
 */
interface Engine {
    /** Probe metadata for [url]. Throws [EngineException] on failure. */
    suspend fun inspect(url: String): InfoResponse

    /** Queue a download and return its job id. */
    suspend fun submit(request: DownloadRequest): String

    /** Current state of [jobId], or null if it is unknown. */
    fun progress(jobId: String): JobProgressResponse?

    /** Cancel [jobId]. Returns false if it was already finished or unknown. */
    fun abort(jobId: String): Boolean

    /** Name of the runtime backing this engine, for /api/capabilities. */
    val runtimeName: String
}

/**
 * A failure that should surface to the client with a specific HTTP status.
 *
 * Mirrors the exception taxonomy in the Python routes: an upstream refusal is
 * 451 with a structured detail, an unparseable URL is 400, anything else is
 * 500.
 */
class EngineException(
    val status: Int,
    override val message: String,
    val code: String? = null,
    val remedy: String? = null,
    val retryAfterSeconds: Int? = null,
    cause: Throwable? = null,
) : Exception(message, cause)