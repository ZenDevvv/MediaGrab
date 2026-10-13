package dev.mediagrab.server

import dev.mediagrab.contract.CapabilitiesResponse
import dev.mediagrab.contract.DownloadRequest
import dev.mediagrab.contract.DownloadsResponse
import dev.mediagrab.contract.ErrorDetail
import dev.mediagrab.contract.JobProgressResponse
import dev.mediagrab.contract.JsRuntimeResponse
import dev.mediagrab.contract.LimitsResponse
import dev.mediagrab.engine.Engine
import dev.mediagrab.engine.EngineException
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCall
import io.ktor.server.application.install
import io.ktor.server.cio.CIO
import io.ktor.server.engine.EmbeddedServer
import io.ktor.server.engine.embeddedServer
import io.ktor.server.plugins.contentnegotiation.ContentNegotiation
import io.ktor.server.request.receive
import io.ktor.server.response.respond
import io.ktor.server.response.respondBytes
import io.ktor.server.response.respondTextWriter
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * The embedded server the WebView talks to.
 *
 * CIO rather than Netty: coroutine-based and materially smaller, which counts
 * directly against the APK size budget (MULTI_PLATFORM_IMPLEMENTATION_PLAN.md
 * §1.1 Decision #7).
 *
 * Every response shape is governed by `openapi-contract.json`. The DTOs in
 * `dev.mediagrab.contract` are annotated to match it, and `ApiContractTest`
 * fails the build if they stop matching.
 */
class ApiServer(
    private val mediaEngine: Engine,
    private val webAssets: WebAssets,
    private val version: String = "dev",
) {
    fun module(): Application.() -> Unit = {
        install(ContentNegotiation) {
            // explicitNulls + encodeDefaults mirror FastAPI's Pydantic output:
            // an absent optional is emitted as null rather than omitted, so the
            // frontend's `!== null` checks behave identically on both servers.
            // ignoreUnknownKeys stops a newer frontend breaking an older app.
            json(
                Json {
                    explicitNulls = true
                    encodeDefaults = true
                    ignoreUnknownKeys = true
                }
            )
        }
        routing {
            // Qualified: inside the routing lambda the receiver is Ktor's Route,
            // which carries its own `engine` property and would otherwise shadow
            // this one -- silently passing an ApplicationEngine where an Engine
            // belongs.
            installApiRoutes(mediaEngine, webAssets, version)
            installAssetRoutes(webAssets)
        }
    }
}

private fun Route.installApiRoutes(
    engine: Engine,
    assets: WebAssets,
    version: String,
) {
    // The frontend calls this before anything else and branches on it, so it
    // must answer even when the media engine is broken.
    get("/api/capabilities") {
        call.respond(
            CapabilitiesResponse(
                mode = "desktop",
                version = version,
                retentionSeconds = 0,
                // Uncapped by design: one user, on their own device, with their
                // own bandwidth and storage. Null means "no limit", which is
                // what the frontend renders.
                limits = LimitsResponse(),
                downloads = DownloadsResponse(),
                // QuickJS ships inside youtubedl-android; there is nothing for
                // the user to install.
                jsRuntimes = JsRuntimeResponse(
                    configured = listOf(engine.runtimeName),
                    available = listOf(engine.runtimeName),
                ),
            )
        )
    }

    post("/api/info") {
        val body = runCatching { call.receive<Map<String, String?>>() }.getOrNull()
        val url = body?.get("url")
        if (url.isNullOrBlank()) {
            call.respondError(HttpStatusCode.BadRequest, "url is required")
        } else {
            call.respondCatching { call.respond(engine.inspect(url)) }
        }
    }

    post("/api/download") {
        val request = runCatching { call.receive<DownloadRequest>() }.getOrNull()
        if (request == null) {
            call.respondError(HttpStatusCode.BadRequest, "malformed request body")
        } else {
            call.respondCatching { call.respond(mapOf("job_id" to engine.submit(request))) }
        }
    }

    get("/api/progress/{jobId}") {
        val state = engine.progress(call.parameters["jobId"].orEmpty())
        if (state == null) {
            call.respondError(HttpStatusCode.NotFound, "unknown job")
        } else {
            call.respond(state)
        }
    }

    post("/api/abort/{jobId}") {
        val jobId = call.parameters["jobId"].orEmpty()
        call.respond(mapOf("aborted" to engine.abort(jobId)))
    }

    // Server-sent events. The frontend subscribes here for live throughput.
    get("/api/events/{jobId}") {
        val jobId = call.parameters["jobId"].orEmpty()
        // As in Python: an unknown job is a 404, not a stream that never ends.
        if (engine.progress(jobId) == null) {
            call.respondError(HttpStatusCode.NotFound, "Job not found")
            return@get
        }
        call.respondTextWriter(contentType = ContentType.Text.EventStream) {
            var last: String? = null
            // Poll rather than push: the engine owns the download threads, and
            // polling keeps this loop cancellable when the WebView navigates
            // away. 400ms is below the point where the bar looks stepped.
            while (true) {
                val state = engine.progress(jobId)
                if (state != null) {
                    val frame = sseFrame(state)
                    if (frame != last) {
                        write(frame)
                        flush()
                        last = frame
                    }
                    if (state.status in TERMINAL_STATUSES) break
                }
                delay(400)
            }
        }
    }

    // Hosted-only in the Python app. A no-op here that still answers, so the
    // frontend's capability-driven UI does not break on a 404.
    post("/api/open-folder") {
        call.respond(mapOf("opened" to false))
    }

    get("/api/file/{jobId}") {
        // Serving bytes is the engine's job on device; until it lands, answer
        // honestly rather than 200 with an empty body.
        call.respondError(HttpStatusCode.NotFound, "no finished file for this job")
    }
}

private val TERMINAL_STATUSES = setOf("ready", "error", "cancelled")

/**
 * One SSE frame, shaped exactly as `api_events` in app/api/routes.py emits it:
 * `complete` carries the file fields, `error` covers both failure and
 * cancellation, and anything else is `progress`. The frontend JSON.parses
 * every `data:` line, so this must be JSON -- it once sent the data class's
 * toString(), which the contract tests could not see (SSE is not in the
 * OpenAPI document) and which the UI survived only by falling back to polling.
 */
internal fun sseFrame(state: JobProgressResponse): String {
    val (event, data) = when (state.status) {
        "ready" -> "complete" to buildJsonObject {
            put("filename", state.filename)
            put("size", state.size)
            put("download_url", state.downloadUrl)
            put("srt_filename", state.srtFilename)
            put("srt_url", state.srtUrl)
        }
        "error" -> "error" to buildJsonObject { put("error", state.error ?: "Unknown failure") }
        "cancelled" -> "error" to buildJsonObject { put("error", "Download cancelled") }
        else -> "progress" to buildJsonObject {
            put("status", state.status)
            put("progress", state.progress)
            put("speed", state.speed)
            put("eta", state.eta)
            put("got", state.got)
            put("stage", state.stage)
            put("queue_position", state.queuePosition)
            put("active_slots", state.activeSlots)
        }
    }
    return "event: $event\ndata: $data\n\n"
}

private fun Route.installAssetRoutes(assets: WebAssets) {
    val pages = mapOf(
        "/" to "index.html",
        "/choose" to "choose.html",
        "/choose.html" to "choose.html",
        "/downloads" to "downloads.html",
    )
    for ((path, file) in pages) {
        get(path) { call.serveAsset(assets, file) }
    }

    // Everything else: css, js, icons, the service worker. The tailcard is last
    // so the explicit routes above win.
    //
    // A *named* tailcard ({path...}) rather than the anonymous {/...} form: the
    // anonymous variant binds its segments to a parameter named "", and
    // getAll("") returned nothing, so every asset request silently fell through
    // to the index.html fallback. The symptom looked like a working server
    // returning the wrong page for every static file.
    get("/{path...}") {
        val path = call.parameters.getAll("path")
            ?.filter { it.isNotEmpty() }
            ?.joinToString("/")
            .orEmpty()
        call.serveAsset(assets, if (path.isEmpty()) "index.html" else path)
    }
}

private suspend fun ApplicationCall.serveAsset(assets: WebAssets, name: String) {
    val bytes = assets.load(name)
    if (bytes == null) {
        // Unknown paths fall back to the landing page rather than a bare 404,
        // matching how the PWA's own navigation behaves.
        val fallback = assets.load("index.html")
        if (fallback == null) {
            respondError(HttpStatusCode.NotFound, "not found")
        }
        else respondBytes(fallback, ContentType.Text.Html)
        return
    }
    respondBytes(bytes, contentTypeFor(name))
}

private fun contentTypeFor(name: String): ContentType = when (name.substringAfterLast('.')) {
    "html" -> ContentType.Text.Html
    "js" -> ContentType.Application.JavaScript
    "css" -> ContentType.Text.CSS
    "json", "webmanifest" -> ContentType.Application.Json
    "svg" -> ContentType.Image.SVG
    "png" -> ContentType.Image.PNG
    "ico" -> ContentType.parse("image/x-icon")
    else -> ContentType.Application.OctetStream
}

private suspend fun ApplicationCall.respondCatching(block: suspend () -> Unit) {
    try {
        block()
    } catch (t: Throwable) {
        when (t) {
            is EngineException -> respondError(
                HttpStatusCode.fromValue(t.status), t.message,
                t.code, t.remedy, t.retryAfterSeconds,
            )
            else -> respondError(
                HttpStatusCode.InternalServerError,
                t.message ?: "unexpected error",
            )
        }
    }
}

private suspend fun ApplicationCall.respondError(
    status: HttpStatusCode,
    message: String,
    code: String? = null,
    remedy: String? = null,
    retryAfterSeconds: Int? = null,
) {
    // FastAPI's shape is {"detail": …}. The frontend reads `detail`, so a bare
    // {"message": …} would render as undefined on Android and work on desktop.
    respond(
        status,
        mapOf(
            "detail" to ErrorDetail(
                message = message,
                code = code,
                remedy = remedy,
                retryAfterSeconds = retryAfterSeconds,
            )
        )
    )
}

/**
 * Start on loopback and return the server. [port] 0 picks an ephemeral port,
 * which is what the tests want; the app asks for a fixed one (see Backend).
 */
fun ApiServer.start(wait: Boolean = false, port: Int = 0): EmbeddedServer<*, *> {
    val server = embeddedServer(CIO, port = port, host = "127.0.0.1", module = module())
    server.start(wait = wait)
    return server
}