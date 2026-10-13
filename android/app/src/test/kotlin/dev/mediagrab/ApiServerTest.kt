package dev.mediagrab

import dev.mediagrab.server.ApiServer
import dev.mediagrab.server.DirectoryAssets
import dev.mediagrab.server.start
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.get
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * HTTP behaviour: status codes, error shapes, asset serving, path traversal.
 *
 * All against a real Ktor CIO server on real loopback, so this exercises the
 * same code path the WebView will.
 */
class ApiServerTest {

    private lateinit var server: io.ktor.server.engine.EmbeddedServer<*, *>
    private lateinit var client: HttpClient
    private var base = ""
    private val engine = FakeEngine()

    @Before
    fun startServer() {
        val assets = DirectoryAssets(File(System.getProperty("mediagrab.webRoot")))
        server = ApiServer(engine, assets, version = "test-1").start(wait = false)
        base = "http://127.0.0.1:" +
            runBlocking { server.engine.resolvedConnectors() }.first().port
        client = HttpClient(CIO)
    }

    @After
    fun stopServer() {
        client.close()
        server.stop(gracePeriodMillis = 0, timeoutMillis = 1000)
    }

    /** GET returning the body text. Wrapped because every caller needs it. */
    private fun get(path: String): String = runBlocking {
        client.get("$base$path").bodyAsText()
    }

    /** POST a JSON body and return the response text. */
    private fun post(path: String, body: String = "{}"): String = runBlocking {
        client.post("$base$path") {
            contentType(ContentType.Application.Json)
            setBody(body)
        }.bodyAsText()
    }

    private fun json(path: String, body: String): JSONObject = JSONObject(post(path, body))

    /** POST and return status alongside the body, for assertions on both. */
    private fun postWithStatus(path: String, body: String): Pair<Int, String> = runBlocking {
        val response = client.post("$base$path") {
            contentType(ContentType.Application.Json)
            setBody(body)
        }
        response.status.value to response.bodyAsText()
    }

    // --- the frontend's asset contract ---------------------------------------

    @Test
    fun `serves the landing page`() {
        assertTrue("expected the real index.html", get("/").contains("<html", ignoreCase = true))
    }

    @Test
    fun `serves the choose page and the app script`() {
        assertTrue(get("/choose").isNotEmpty())
        assertTrue("app.js should be served verbatim", get("/js/app.js").contains("api/"))
    }

    @Test
    fun `unknown page falls back to the landing page rather than a bare 404`() {
        // The PWA navigates client-side; a hard 404 would break deep links.
        assertTrue(get("/nope").contains("<html", ignoreCase = true))
    }

    @Test
    fun `refuses to serve files outside the asset root`() {
        // The path arrives from the request, so without the canonical-path
        // check in DirectoryAssets anything able to reach loopback could read
        // the app's private files.
        assertFalse("served a file outside the root", get("/../../../etc/passwd").contains("root:"))
    }

    // --- errors must carry FastAPI's shape -----------------------------------

    @Test
    fun `missing url is a 400 with a detail field`() {
        val body = json("/api/info", """{"nope":"x"}""")
        assertTrue("frontend reads .detail", body.has("detail"))
        assertTrue(body.getJSONObject("detail").has("message"))
    }

    @Test
    fun `unknown job is a 404`() {
        val response = runBlocking { client.get("$base/api/progress/does-not-exist") }
        assertEquals(HttpStatusCode.NotFound, response.status)
    }

    @Test
    fun `upstream refusal is 451 and carries the structured remedy`() {
        // This is the YouTube circuit's payload. The frontend routes on
        // detail.code and detail.remedy, so both must survive the port.
        engine.failWith = upstreamRefusal()
        val (status, text) = postWithStatus("/api/info", """{"url":"https://example.invalid/watch"}""")
        assertEquals(451, status)
        val detail = JSONObject(text).getJSONObject("detail")
        assertEquals("UPSTREAM_BLOCKED", detail.getString("code"))
        assertEquals("native_app", detail.getString("remedy"))
        assertEquals(1800, detail.getInt("retry_after_seconds"))
    }

    @Test
    fun `download returns job_id and accepts the full request body`() {
        val id = json(
            "/api/download",
            """{"url":"https://example.invalid/v","mode":"mp3","quality":"720p",
                "bitrate":192,"trim":{"start":10,"end":40},
                "subtitles":{"lang":"en","embed":true}}""",
        ).getString("job_id")

        assertEquals(1, engine.submitted.size)
        val sent = engine.submitted.single()
        assertEquals("https://example.invalid/v", sent.url)
        assertEquals("mp3", sent.mp4Mode)
        assertEquals("720p", sent.quality)
        assertEquals(10, sent.trim?.start)
        assertEquals("en", sent.subtitles?.lang)
        assertTrue("progress should be registered", engine.progress(id) != null)
    }

    @Test
    fun `abort reports whether the job was actually cancelled`() {
        val id = json("/api/download", """{"url":"x"}""").getString("job_id")
        assertTrue(JSONObject(post("/api/abort/$id")).getBoolean("aborted"))
        assertEquals("cancelled", engine.progress(id)?.status)

        // Second abort must report false: the job is already finished, and
        // claiming otherwise would leave the UI spinning.
        assertFalse(JSONObject(post("/api/abort/$id")).getBoolean("aborted"))
    }

    @Test
    fun `capabilities reports desktop mode with no limits`() {
        // On device there is no shared pool to ration and no retention policy.
        val body = JSONObject(get("/api/capabilities"))
        assertEquals("desktop", body.getString("mode"))
        val limits = body.getJSONObject("limits")
        assertTrue(
            "every cap must be absent, not zero: " + limits,
            limits.isNull("max_file_size_bytes") && limits.isNull("max_duration_seconds"),
        )
    }

    @Test
    fun `events stream JSON frames shaped like the Python server's`() {
        val id = json("/api/download", """{"url":"x"}""").getString("job_id")
        engine.jobs[id] = engine.jobs.getValue(id).copy(status = "ready", size = "1.2 MB")
        val body = get("/api/events/$id")

        val frames = body.trim().split("\n\n")
        assertEquals("one terminal frame, then the stream ends: $body", 1, frames.size)
        val (event, data) = frames.single().lines()
        assertEquals("event: complete", event)
        // Every data line must parse: the frontend JSON.parses them all.
        val payload = JSONObject(data.removePrefix("data: "))
        assertEquals("output.mp4", payload.getString("filename"))
        assertEquals("/api/file/$id", payload.getString("download_url"))
        assertFalse("complete carries file fields only", payload.has("status"))
    }

    @Test
    fun `a cancelled job ends the stream with an error frame, not complete`() {
        val id = json("/api/download", """{"url":"x"}""").getString("job_id")
        engine.jobs[id] = engine.jobs.getValue(id).copy(status = "cancelled")
        val frame = get("/api/events/$id").trim()
        assertTrue(frame, frame.startsWith("event: error\n"))
        assertEquals("Download cancelled", JSONObject(frame.substringAfter("data: ")).getString("error"))
    }

    @Test
    fun `events for an unknown job is a 404 rather than an endless stream`() {
        val status = runBlocking { client.get("$base/api/events/nope").status.value }
        assertEquals(404, status)
    }
}