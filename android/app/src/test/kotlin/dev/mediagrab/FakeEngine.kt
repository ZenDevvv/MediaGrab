package dev.mediagrab

import dev.mediagrab.contract.InfoResponse
import dev.mediagrab.contract.JobProgressResponse
import dev.mediagrab.contract.QualityOption
import dev.mediagrab.engine.Engine
import dev.mediagrab.engine.EngineException
import dev.mediagrab.engine.StoredFile
import dev.mediagrab.engine.mimeTypeFor
import dev.mediagrab.contract.DownloadRequest

/**
 * A scripted stand-in for `youtubedl-android`.
 *
 * Lets every HTTP behaviour be tested on the JVM — status codes, SSE framing,
 * error shapes, JSON field names — without an emulator. The real engine needs
 * a device; the server does not.
 */
class FakeEngine(
    override val runtimeName: String = "quickjs",
) : Engine {
    val submitted = mutableListOf<DownloadRequest>()
    val aborted = mutableListOf<String>()

    var infoResult: (String) -> InfoResponse = { url ->
        InfoResponse(url = url, title = "Fake title", uploader = "Fake uploader", duration = 61)
    }
    var jobs = mutableMapOf<String, JobProgressResponse>()
    var failWith: Throwable? = null
    /** Bytes served for a ready job, keyed by job id. */
    val files = mutableMapOf<String, ByteArray>()
    private var jobCount = 0

    override suspend fun inspect(url: String): InfoResponse {
        failWith?.let { throw it }
        return infoResult(url)
    }

    override suspend fun submit(request: DownloadRequest): String {
        failWith?.let { throw it }
        submitted += request
        val id = "job-${++jobCount}"
        jobs[id] = JobProgressResponse(
            jobId = id,
            status = "queued",
            filename = "output.mp4",
            downloadUrl = "/api/file/$id",
        )
        return id
    }

    override fun progress(jobId: String): JobProgressResponse? = jobs[jobId]

    override fun file(jobId: String): StoredFile? {
        if (jobs[jobId]?.status != "ready") return null
        val bytes = files[jobId] ?: return null
        return StoredFile("Me at the zoo.mp4", mimeTypeFor("x.mp4"), bytes.size.toLong()) { bytes.inputStream() }
    }

    override fun abort(jobId: String): Boolean {
        aborted += jobId
        val job = jobs[jobId] ?: return false
        if (job.status in setOf("ready", "error", "cancelled")) return false
        jobs[jobId] = job.copy(status = "cancelled")
        return true
    }
}

/** Sample info payload with every optional populated, for shape checks. */
val fullyPopulatedInfo = InfoResponse(
    url = "https://example.invalid/watch",
    title = "A title",
    uploader = "A channel",
    duration = 212,
    thumbnail = "https://example.invalid/t.jpg",
    sourceAudioKbps = 192,
    sourceAudioCodec = "aac",
    qualities = listOf(
        QualityOption(resolution = "1080p", approxBytes = 12345678, type = "remux"),
        QualityOption(resolution = "720p", approxBytes = 7654321, type = "remux"),
    ),
)

/** The error the YouTube circuit produces, as the Python app emits it. */
fun upstreamRefusal(): EngineException = EngineException(
    status = 451,
    message = "YouTube limits requests from shared servers, so most links won't work here.",
    code = "UPSTREAM_BLOCKED",
    remedy = "native_app",
    retryAfterSeconds = 1800,
)