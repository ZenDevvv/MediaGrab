package dev.mediagrab

import android.app.Application
import android.util.Log
import dev.mediagrab.engine.YoutubeDlEngine
import dev.mediagrab.server.ApiServer
import dev.mediagrab.server.ApkAssets
import dev.mediagrab.server.start
import io.ktor.server.engine.EmbeddedServer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import java.io.File

/**
 * Owns the embedded backend for the life of the process.
 *
 * The server lives here rather than in the activity so that recreating the
 * activity cannot start a second server or orphan the jobs the first one was
 * tracking. Started lazily, on first request for [backend], and never stopped:
 * the process dying is what stops it. Keeping it alive while the app is in the
 * background is the foreground service's job (plan §5 M4 step 5).
 */
class MediaGrabApp : Application() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** The base URL the WebView should load, once the server is listening. */
    val backend: Deferred<String> by lazy { scope.async { startBackend() } }

    private suspend fun startBackend(): String {
        // Private staging until MediaStore saving lands (step 5).
        val engine = YoutubeDlEngine(this, File(filesDir, "downloads"))
        try {
            engine.initialise()
        } catch (t: Throwable) {
            // Start the server regardless. /api/capabilities must answer even
            // with a broken engine, and an error from /api/info is something the
            // frontend can show; a page that never loads is not.
            Log.e(TAG, "engine initialisation failed", t)
        }

        val api = ApiServer(
            engine,
            ApkAssets { path -> assets.open("web/$path") },
            version = packageManager.getPackageInfo(packageName, 0).versionName ?: "dev",
        )
        val server = bind(api)
        val port = server.engine.resolvedConnectors().first().port
        Log.i(TAG, "backend listening on 127.0.0.1:$port")
        return "http://127.0.0.1:$port/"
    }

    /**
     * A fixed port, because the port is part of the page's origin: on an
     * ephemeral one, every launch would be a new origin and the frontend's
     * localStorage (theme, history) would be empty each time. Falls back to
     * ephemeral rather than failing if something else holds it.
     */
    private fun bind(api: ApiServer): EmbeddedServer<*, *> =
        try {
            api.start(port = PREFERRED_PORT)
        } catch (t: Throwable) {
            Log.w(TAG, "port $PREFERRED_PORT unavailable, using an ephemeral one", t)
            api.start(port = 0)
        }

    private companion object {
        const val TAG = "MediaGrab"
        const val PREFERRED_PORT = 47813
    }
}
