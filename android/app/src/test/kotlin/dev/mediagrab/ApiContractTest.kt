package dev.mediagrab

import dev.mediagrab.contract.CapabilitiesResponse
import dev.mediagrab.contract.JobProgressResponse
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
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * The Android server must answer exactly as the Python one does.
 *
 * `openapi-contract.json` is the shared source of truth. These tests serve
 * real responses over real loopback HTTP and compare the emitted JSON against
 * that file, so a Kotlin field renamed or dropped fails the build here instead
 * of rendering a blank WebView on a phone.
 *
 * This runs on the JVM. Ktor CIO is not Android-specific, and the engine is
 * behind an interface, so an emulator is not needed to prove the HTTP contract.
 */
class ApiContractTest {

    private lateinit var server: io.ktor.server.engine.EmbeddedServer<*, *>
    private lateinit var client: HttpClient
    private var base = ""

    private val contract: JsonObject by lazy {
        // The same file tests/test_api_contract.py asserts against, read from
        // the repository rather than a private copy.
        val file = File(System.getProperty("mediagrab.contract"))
        check(file.isFile) { "${file.path} not found; run dev/sync-contract.py" }
        Json.parseToJsonElement(file.readText()).jsonObject
    }

    private val engine = FakeEngine()

    /**
     * The real frontend tree, served from the repository's static/ directory.
     * Pointing the server at the actual assets rather than fixtures means the
     * asset routes are tested against what will really ship in the APK.
     */
    private val webRoot: File = File(System.getProperty("mediagrab.webRoot"))

    @Before
    fun startServer() {
        val api = ApiServer(engine, DirectoryAssets(webRoot), version = "test-1")
        server = api.start(wait = false)
        // Port 0 was requested, so the bound port is only knowable after
        // start, and resolvedConnectors() is suspend.
        val port = runBlocking { server.engine.resolvedConnectors() }.first().port
        base = "http://127.0.0.1:$port"
        client = HttpClient(CIO)
    }

    @After
    fun stopServer() {
        client.close()
        server.stop(gracePeriodMillis = 0, timeoutMillis = 1000)
    }

    private fun schemaFor(name: String): JsonObject =
        contract["components"]!!.jsonObject["schemas"]!!.jsonObject[name]!!.jsonObject

    private fun contractProperties(name: String): Set<String> =
        schemaFor(name)["properties"]?.jsonObject?.keys?.toSet() ?: emptySet()

    // --- the naming rule that motivates all of this --------------------------

    @Test
    fun `progress response emits snake_case exactly as the contract declares`() {
        runBlocking {
            engine.jobs["job-1"] = JobProgressResponse(
                jobId = "job-1",
                status = "ready",
                filename = "clip.mp4",
                downloadUrl = "/api/file/job-1",
                srtFilename = "clip.srt",
                srtUrl = "/api/file/job-1.srt",
                queuePosition = 0,
                activeSlots = 1,
            )
            val body = JSONObject(
                client.get("$base/api/progress/job-1").bodyAsText()
            )

            // The specific drift this whole arrangement exists to prevent.
            val offending = body.keys().asSequence().filter { it.any(Char::isUpperCase) }.toList()
            assertTrue(
                "camelCase leaked into the response: $offending. Kotlin's default " +
                    "property naming is camelCase and the frontend reads snake_case, " +
                    "so a missing @SerialName makes these fields undefined in the " +
                    "WebView with no error anywhere.",
                offending.isEmpty()
            )
            for (field in listOf(
                "job_id", "download_url", "srt_url", "srt_filename",
                "queue_position", "active_slots",
            )) {
                assertTrue("$field missing from $body", body.has(field))
            }
        }
    }

    // --- every response validated against the snapshot -----------------------

    @Test
    fun `capabilities response matches the pinned contract`() {
        runBlocking {
            val text = client.get("$base/api/capabilities").bodyAsText()
            assertShape(text, "CapabilitiesResponse")
            val body = JSONObject(text)
            assertEquals("desktop", body.getString("mode"))
            assertEquals("test-1", body.getString("version"))
            assertTrue(body.has("js_runtimes"))
        }
    }

    @Test
    fun `info response matches the pinned contract`() {
        runBlocking {
            engine.infoResult = { fullyPopulatedInfo }
            val text = client.post("$base/api/info") {
                contentType(ContentType.Application.Json)
                setBody("""{"url":"https://example.invalid/watch"}""")
            }.bodyAsText()

            assertShape(text, "InfoResponse")
            val body = JSONObject(text)
            assertEquals("A title", body.getString("title"))
            assertEquals(192, body.getInt("source_audio_kbps"))
            assertEquals("aac", body.getString("source_audio_codec"))
            val first = body.getJSONArray("qualities").getJSONObject(0)
            assertEquals("1080p", first.getString("resolution"))
            assertEquals(12345678L, first.getLong("approx_bytes"))
        }
    }

    @Test
    fun `progress response matches the pinned contract`() {
        runBlocking {
            engine.jobs["job-1"] = JobProgressResponse(
                jobId = "job-1", status = "downloading", progress = 0.5,
                speed = "2.1MiB/s", eta = "00:12", stage = "downloading",
            )
            assertShape(client.get("$base/api/progress/job-1").bodyAsText(), "JobProgressResponse")
        }
    }

    /**
     * Compare an emitted payload's keys against the contract.
     *
     * Exact match in both directions. A superset would mean the frontend is
     * being offered fields the Python app never sends; a subset means it is
     * being asked for fields that will be undefined.
     */
    private fun assertShape(text: String, schema: String) {
        val body = JSONObject(text)
        val declared = contractProperties(schema)
        val emitted = body.keys().asSequence().toSet()

        val extra = (emitted - declared).sorted()
        val missing = (declared - emitted).sorted()
        assertTrue("$schema emits fields the contract does not declare: $extra", extra.isEmpty())
        assertTrue("$schema omits declared fields: $missing", missing.isEmpty())

        // Required in the contract must be present and non-null.
        for (field in schemaFor(schema)["required"]?.jsonArray
            ?.map { it.jsonPrimitive.content } ?: emptyList()) {
            assertNotNull("$schema.$field is null", body.opt(field))
        }
    }
}