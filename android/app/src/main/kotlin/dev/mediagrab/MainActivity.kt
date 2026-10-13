package dev.mediagrab

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

/**
 * A WebView pointed at the embedded server -- the whole UI is the same
 * `static/` frontend the desktop and hosted builds serve, unchanged.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Inspectable from chrome://inspect on debug builds only. This is the
        // one window into the frontend on a device, and it must not ship.
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            // app.js keeps theme and history in localStorage.
            settings.domStorageEnabled = true
            webViewClient = LoopbackOnlyClient()
        }
        setContentView(webView)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        // First launch extracts the Python runtime, which takes a few seconds;
        // show something rather than a blank white view.
        webView.loadData(STARTING_PAGE, "text/html", "utf-8")
        lifecycleScope.launch {
            val url = try {
                (application as MediaGrabApp).backend.await()
            } catch (e: CancellationException) {
                // The activity went away while waiting -- WebView's first load
                // relaunches it on some versions. Not a failure; the new
                // instance awaits the same backend.
                throw e
            } catch (t: Throwable) {
                Log.e("MediaGrab", "backend failed to start", t)
                webView.loadData(FAILED_PAGE, "text/html", "utf-8")
                return@launch
            }
            webView.loadUrl(url)
        }
    }

    /**
     * Keeps the WebView on the embedded server. Anything else -- the source
     * video's page, the project's links -- opens in the user's browser, where
     * it has their sign-ins and where a stray page cannot reach loopback with
     * the app's origin.
     */
    private inner class LoopbackOnlyClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val uri = request.url
            if (uri.host == "127.0.0.1") return false
            openExternally(uri)
            return true
        }
    }

    private fun openExternally(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (_: ActivityNotFoundException) {
            Log.w("MediaGrab", "no app to open $uri")
        }
    }

    private companion object {
        const val STARTING_PAGE =
            "<html><body style=\"font-family:sans-serif;padding:2em;color:#888\">" +
                "Starting MediaGrab&hellip;</body></html>"
        const val FAILED_PAGE =
            "<html><body style=\"font-family:sans-serif;padding:2em\">" +
                "MediaGrab could not start its local server. " +
                "Restart the app; if it keeps happening, the log has the cause.</body></html>"
    }
}
