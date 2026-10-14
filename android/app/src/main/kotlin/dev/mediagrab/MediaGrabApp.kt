package dev.mediagrab

import android.app.Application
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.util.Log
import dev.mediagrab.engine.YoutubeDlEngine
import dev.mediagrab.server.ApiServer
import dev.mediagrab.server.ApkAssets
import dev.mediagrab.server.startPreferring
import dev.mediagrab.storage.DownloadsPublisher
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

    /** The download engine. Shared with [DownloadService], which watches it. */
    val engine: YoutubeDlEngine by lazy {
        // Private staging; finished files are published to Downloads.
        YoutubeDlEngine(
            this, File(filesDir, "downloads"), DownloadsPublisher(this), BuildConfig.YTDLP_VERSION,
        ).also {
            it.onActivityChanged = {
                // Only ever *starts* the service: it stops itself once the
                // engine reports nothing active, so no stop races a new job.
                if (it.activeCount() > 0) startDownloadService()
            }
        }
    }

    /** The base URL the WebView should load, once the server is listening. */
    val backend: Deferred<String> by lazy { scope.async { startBackend() } }

    private suspend fun startBackend(): String {
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
            revealDownloads = ::revealDownloads,
        )
        // Fixed, because the port is part of the page origin: an ephemeral one
        // would be a new origin each launch and empty localStorage (theme,
        // history). Falls back if another app holds it; see startPreferring.
        val server = api.startPreferring(PREFERRED_PORT)
        val port = server.engine.resolvedConnectors().first().port
        Log.i(TAG, "backend listening on 127.0.0.1:$port")
        return "http://127.0.0.1:$port/"
    }

    /**
     * Best effort. Android 12+ refuses a foreground-service start while the app
     * is in the background, and that refusal used to escape through submit():
     * the request answered 500 while the job it had already queued ran on and
     * saved a file the client had been told failed. Found on the emulator. The
     * download matters more than the notification, so a refusal is logged and
     * the job proceeds -- it just loses protection from being killed.
     */
    private fun startDownloadService() {
        try {
            DownloadService.start(this)
        } catch (e: IllegalStateException) {
            // ForegroundServiceStartNotAllowedException is a subclass.
            Log.w(TAG, "download service not started; continuing without it", e)
        }
    }

    /** The system Downloads app, as desktop opens its file manager. */
    private fun revealDownloads(): Boolean =
        try {
            startActivity(
                Intent(DownloadManager.ACTION_VIEW_DOWNLOADS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            true
        } catch (t: ActivityNotFoundException) {
            Log.w(TAG, "no Downloads app to open", t)
            false
        }

    private companion object {
        const val TAG = "MediaGrab"
        const val PREFERRED_PORT = 47813
    }
}
