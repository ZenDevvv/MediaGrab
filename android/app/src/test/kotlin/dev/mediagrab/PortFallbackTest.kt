package dev.mediagrab

import dev.mediagrab.server.ApiServer
import dev.mediagrab.server.DirectoryAssets
import dev.mediagrab.server.startPreferring
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.get
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket

/**
 * The app binds a fixed loopback port and falls back to an ephemeral one if
 * something else holds it. The fallback only works if a taken port actually
 * surfaces as a BindException from start() -- until this test, nothing had
 * shown that it does.
 */
class PortFallbackTest {

    private fun api() = ApiServer(FakeEngine(), DirectoryAssets(File(System.getProperty("mediagrab.webRoot"))))

    private fun portOf(server: io.ktor.server.engine.EmbeddedServer<*, *>) =
        runBlocking { server.engine.resolvedConnectors().first().port }

    @Test
    fun `binds the preferred port when it is free`() {
        val free = ServerSocket(0, 1, InetAddress.getLoopbackAddress()).use { it.localPort }
        val server = api().startPreferring(free)
        try {
            assertEquals(free, portOf(server))
        } finally {
            server.stop(0, 1000)
        }
    }

    @Test
    fun `falls back to another port, and still serves, when the preferred one is taken`() {
        ServerSocket(0, 1, InetAddress.getLoopbackAddress()).use { squatter ->
            val server = api().startPreferring(squatter.localPort)
            try {
                val port = portOf(server)
                assertNotEquals(squatter.localPort, port)
                val status = runBlocking {
                    HttpClient(CIO).use { it.get("http://127.0.0.1:$port/api/capabilities").status.value }
                }
                assertEquals(200, status)
            } finally {
                server.stop(0, 1000)
            }
        }
    }
}
