package dev.mediagrab.storage

import android.content.ContentValues
import android.content.Context
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import dev.mediagrab.engine.mimeTypeFor
import java.io.File
import java.io.FileInputStream
import java.io.InputStream

/**
 * Moves a finished download into the user's public Downloads, where the
 * desktop app writes it too (plan §1.6), so the ready screen's "Open in
 * Downloads" is true on every platform.
 *
 * Two paths, because minSdk is 26 and scoped storage arrived in 29:
 *  - 29+: MediaStore.Downloads. No permission needed for files the app
 *    itself creates, and the provider de-duplicates names ("Title (1).mp4").
 *  - 26-28: the public directory directly, which needs WRITE_EXTERNAL_STORAGE
 *    (requested by MainActivity) and does its own de-duplication to match.
 */
class DownloadsPublisher(context: Context) {

    private val appContext = context.applicationContext
    private val resolver = appContext.contentResolver

    /** Where a published file ended up. [name] is what the user will see. */
    class Published(val uri: Uri, val name: String, val size: Long)

    fun publish(source: File, onProgress: (Double) -> Unit = {}): Published =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) publishToMediaStore(source, onProgress)
        else publishToPublicDirectory(source, onProgress)

    fun open(uri: Uri): InputStream =
        if (uri.scheme == "file") FileInputStream(File(requireNotNull(uri.path)))
        else requireNotNull(resolver.openInputStream(uri)) { "cannot open $uri" }

    private fun publishToMediaStore(source: File, onProgress: (Double) -> Unit): Published {
        val collection = MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, source.name)
            put(MediaStore.Downloads.MIME_TYPE, mimeTypeFor(source.name))
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
            // Hidden from other apps until the copy completes, so nothing can
            // pick up a half-written file.
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = requireNotNull(resolver.insert(collection, values)) {
            "MediaStore refused to create ${source.name}"
        }
        try {
            requireNotNull(resolver.openOutputStream(uri)) { "cannot write $uri" }.use { out ->
                source.inputStream().use { copyReporting(it, out, source.length(), onProgress) }
            }
            resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        } catch (t: Throwable) {
            // A pending row left behind shows up as a phantom zero-byte file.
            resolver.delete(uri, null, null)
            throw t
        }
        // Read the name back: the provider renames on collision, and the ready
        // screen should show the name the user will actually find.
        val name = resolver.query(uri, arrayOf(MediaStore.Downloads.DISPLAY_NAME), null, null, null)
            ?.use { if (it.moveToFirst()) it.getString(0) else null }
            ?: source.name
        return Published(uri, name, source.length())
    }

    @Suppress("DEPRECATION") // The supported path below API 29.
    private fun publishToPublicDirectory(source: File, onProgress: (Double) -> Unit): Published {
        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        dir.mkdirs()
        val target = uniqueTarget(dir, source.name)
        source.inputStream().use { input ->
            target.outputStream().use { copyReporting(input, it, source.length(), onProgress) }
        }
        // Without a scan the file exists but the Downloads app and galleries
        // do not list it until something else triggers indexing.
        MediaScannerConnection.scanFile(appContext, arrayOf(target.path), arrayOf(mimeTypeFor(target.name)), null)
        return Published(Uri.fromFile(target), target.name, target.length())
    }
}

/**
 * `Title.mp4`, then `Title (1).mp4`, `Title (2).mp4` -- the same scheme as
 * desktop's de-duplication and MediaStore's own.
 */
internal fun uniqueTarget(dir: File, name: String): File {
    val first = File(dir, name)
    if (!first.exists()) return first
    val stem = name.substringBeforeLast('.')
    val ext = name.substringAfterLast('.', "").let { if (it.isEmpty()) "" else ".$it" }
    var n = 1
    while (true) {
        val candidate = File(dir, "$stem ($n)$ext")
        if (!candidate.exists()) return candidate
        n++
    }
}

/** [InputStream.copyTo] that reports the fraction copied, for the saving step. */
internal fun copyReporting(input: InputStream, out: java.io.OutputStream, total: Long, onProgress: (Double) -> Unit) {
    val buffer = ByteArray(256 * 1024)
    var copied = 0L
    while (true) {
        val n = input.read(buffer)
        if (n < 0) break
        out.write(buffer, 0, n)
        copied += n
        if (total > 0) onProgress(copied.toDouble() / total)
    }
}
