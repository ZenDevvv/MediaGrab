package dev.mediagrab

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Keeps the process alive while downloads run.
 *
 * Without it, switching away from the app lets Android freeze or kill the
 * process mid-download, and with it the embedded Python and the job. The
 * service does no work itself -- the engine owns the downloads -- it only
 * holds a foreground notification while any job is active, then stops.
 */
class DownloadService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var watching = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // startForeground must be called promptly after startForegroundService,
        // on every start, or the system kills the app with an ANR-like crash.
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification(engine().activeCount(), engine().leadProgress()),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0,
        )
        if (!watching) {
            watching = true
            scope.launch { watch() }
        }
        // Not sticky: if the process dies, the jobs died with it, and there is
        // nothing for a restarted service to resume.
        return START_NOT_STICKY
    }

    private suspend fun watch() {
        val manager = getSystemService(NotificationManager::class.java)
        while (true) {
            val active = engine().activeCount()
            if (active == 0) break
            manager.notify(NOTIFICATION_ID, notification(active, engine().leadProgress()))
            delay(1000)
        }
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
        watching = false
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun engine() = (application as MediaGrabApp).engine

    private fun notification(active: Int, progress: Int?): Notification {
        ensureChannel(this)
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(if (active == 1) "Downloading" else "Downloading $active files")
            .setProgress(100, progress ?: 0, progress == null)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "downloads"
        private const val NOTIFICATION_ID = 1

        /** Start (or refresh) the service. Safe to call repeatedly. */
        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, DownloadService::class.java))
        }

        private fun ensureChannel(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL_ID) != null) return
            // Low importance: a progress bar, not something that should buzz.
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Downloads", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }
}
