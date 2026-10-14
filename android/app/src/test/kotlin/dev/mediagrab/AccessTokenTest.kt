package dev.mediagrab

import dev.mediagrab.server.ApiServer
import dev.mediagrab.server.DirectoryAssets
import dev.mediagrab.server.TOKEN_COOKIE
import dev.mediagrab.server.start
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.statement.bodyAsText
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * Any app on the phone can reach loopback. With a token configured, the API
 * answers only requests carrying it -- which only the app's own WebView has.
 */
class AccessTokenTest {

    private val token = "a".repeat(64)
    private val engine = FakeEngine()
    private lateinit var server: io.ktor.server.engine.EmbeddedServer<*, *>
    private lateinit var client: HttpClient
    private var base = ""

    @Before
    fun startServer() {
        val assets = DirectoryAssets(File(System.getProperty("mediagrab.webRoot")))
        server = ApiServer(engine, assets, accessToken = token).start()
        base = "http://127.0.0.1:" + runBlocking { server.engine.resolvedConnectors() }.first().port
        client = HttpClient(CIO)
    }

    @After
    fun stopServer() {
        client.close()
        server.stop(0, 1000)
    }

    private fun status(path: String, cookie: String? = null, post: Boolean = false): Int = runBlocking {
        val block: io.ktor.client.request.HttpRequestBuilder.() -> Unit = {
            cookie?.let { header("Cookie", it) }
        }
        (if (post) client.post("$base$path", block) else client.get("$base$path", block)).status.value
    }

    @Test
    fun `api calls without the cookie are refused`() {
        assertEquals(403, status("/api/capabilities"))
        assertEquals(403, status("/api/file/anything"))
        assertEquals(403, status("/api/download", post = true))
        assertTrue("nothing may be queued by a refused caller", engine.submitted.isEmpty())
    }

    @Test
    fun `a wrong token is refused`() {
        assertEquals(403, status("/api/capabilities", "$TOKEN_COOKIE=${"b".repeat(64)}"))
        assertEquals(403, status("/api/capabilities", "$TOKEN_COOKIE="))
    }

    @Test
    fun `the right token is let through`() {
        assertEquals(200, status("/api/capabilities", "$TOKEN_COOKIE=$token"))
        // Alongside other cookies, as a browser sends them.
        assertEquals(200, status("/api/capabilities", "theme=dark; $TOKEN_COOKIE=$token"))
    }

    @Test
    fun `the refusal has the shape the frontend reads`() {
        val body = runBlocking { client.get("$base/api/capabilities").bodyAsText() }
        assertEquals("Not allowed", JSONObject(body).getJSONObject("detail").getString("message"))
    }

    @Test
    fun `the static frontend stays public`() {
        // It must load before the page can make its first API call.
        assertEquals(200, status("/"))
        assertEquals(200, status("/js/app.js"))
    }
}
