package dev.mediagrab.server

/**
 * Loads the bundled web frontend.
 *
 * Abstracted because the two hosts disagree: on-device the files live in the
 * APK's assets directory, while the JVM tests read from disk. The server takes
 * a [WebAssets] rather than touching `android.content.res.AssetManager`
 * directly, which is what keeps it off the Android dependency tree and
 * therefore testable without a device.
 */
interface WebAssets {
    /** Bytes for [path] (e.g. `index.html`, `js/app.js`), or null if absent. */
    suspend fun load(path: String): ByteArray?
}

/**
 * Serves the APK's `assets/web` directory.
 *
 * `assets.open()` only finds files at the top of the asset tree, so paths are
 * resolved relative to the `web/` prefix by the caller.
 */
class ApkAssets(
    private val open: (String) -> java.io.InputStream,
) : WebAssets {
    override suspend fun load(path: String): ByteArray? =
        try {
            open(path).use { it.readBytes() }
        } catch (_: java.io.FileNotFoundException) {
            null
        }
}

/** Reads from a directory on the local filesystem. Used by the JVM tests. */
class DirectoryAssets(private val root: java.io.File) : WebAssets {
    override suspend fun load(path: String): ByteArray? {
        val file = java.io.File(root, path).canonicalFile
        // Refuse to serve anything outside the root. The path arrives from the
        // request, so without this a WebView (or anything else that can reach
        // loopback) could read the app's private files.
        if (!file.path.startsWith(root.canonicalFile.path + java.io.File.separator)) {
            return null
        }
        return if (file.isFile) file.readBytes() else null
    }
}