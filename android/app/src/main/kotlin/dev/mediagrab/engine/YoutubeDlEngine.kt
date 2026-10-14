package dev.mediagrab.engine

import android.content.Context
import android.util.Log
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import dev.mediagrab.contract.DownloadRequest
import dev.mediagrab.contract.InfoResponse
import dev.mediagrab.contract.JobProgressResponse
import dev.mediagrab.contract.QualityOption
import dev.mediagrab.contract.SubtitleOption
import dev.mediagrab.storage.DownloadsPublisher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.math.abs

/**
 * The real engine: `youtubedl-android`, which embeds a Python interpreter and
 * a prebuilt yt-dlp and shells out to them.
 *
 * Verified only up to compilation. Everything here that touches the library
 * itself — process spawning, the progress callback, output parsing, MediaStore
 * — needs a device to confirm, because the library installs its Python runtime
 * into the app's no-backup directory on first use and cannot run on the JVM.
 *
 * The HTTP surface around it *is* covered: `ApiServerTest` drives this
 * interface through `FakeEngine`, and `ApiContractTest` asserts the responses
 * against the pinned contract. That split is deliberate.
 */
class YoutubeDlEngine(
    context: Context,
    /** Private staging. Finished files leave it for public Downloads. */
    private val downloadDir: File,
    private val publisher: DownloadsPublisher,
    /** The yt-dlp version this APK bundles (android/ytdlp.lock). */
    private val bundledYtdlpVersion: String,
) : Engine {

    /**
     * Called on every job state change that matters to the foreground
     * service: a job starting, and a job reaching a terminal state.
     */
    @Volatile var onActivityChanged: () -> Unit = {}

    private val appContext = context.applicationContext
    private val jobs = ConcurrentHashMap<String, MutableJob>()
    private val scope = kotlinx.coroutines.CoroutineScope(Dispatchers.IO)

    override val runtimeName = "quickjs"

    /**
     * Extract the Python runtime and yt-dlp into app storage.
     *
     * Must run before any request: the library spawns `python3` on first use
     * and every call fails until this has completed. Both initialisers are
     * required — `FFmpeg.init` is what puts the postprocessor on PATH, and
     * without it remux, MP3 conversion and subtitle embedding fail silently
     * rather than loudly.
     *
     * Suspends because this copies tens of megabytes and must not run on the
     * main thread.
     */
    suspend fun initialise() = withContext(Dispatchers.IO) {
        refreshBundledYtdlp()
        YoutubeDL.getInstance().init(appContext)
        ytdlpStamp().writeText(bundledYtdlpVersion)
        FFmpeg.getInstance().init(appContext)
        // No job outlives its process, so anything already in staging is left
        // over from one that was killed mid-download. Nothing else would ever
        // remove it.
        downloadDir.deleteRecursively()
        downloadDir.mkdirs()
    }

    /**
     * The library copies yt-dlp out of the APK only when no copy exists yet,
     * so an app update carrying a newer yt-dlp would be ignored on every
     * existing install -- the stale copy is exactly what 403s on YouTube. The
     * stamp records which bundled version the on-device copy came from; on a
     * mismatch the copy is removed and init() installs this APK's.
     */
    private fun refreshBundledYtdlp() {
        val stamp = ytdlpStamp()
        val installed = stamp.takeIf { it.isFile }?.readText()
        if (installed != bundledYtdlpVersion) {
            File(appContext.noBackupFilesDir, "youtubedl-android/yt-dlp").deleteRecursively()
        }
    }

    private fun ytdlpStamp() = File(appContext.noBackupFilesDir, "ytdlp-bundled-version")

    override suspend fun inspect(url: String): InfoResponse = withContext(Dispatchers.IO) {
        // -J dumps a single JSON document and skips the download, which is the
        // cheapest way to get everything the chooser needs in one round trip.
        val request = YoutubeDLRequest(url)
            .addOption("-J")
            .addOption("--no-playlist")
            .addOption("--no-warnings")

        val response = YoutubeDL.getInstance().execute(request)
        if (response.exitCode != 0) {
            throw EngineException(
                status = classifyExit(response.err),
                message = response.err.trim().ifEmpty { "yt-dlp failed" },
            )
        }
        parseMetadata(response.out, url)
    }

    override suspend fun submit(request: DownloadRequest): String {
        val id = UUID.randomUUID().toString()
        val job = MutableJob(id, request, downloadDir)
        jobs[id] = job

        scope.launch {
            try {
                job.status = "extracting"
                runDownload(job)
            } catch (t: Throwable) {
                if (job.status != "cancelled") {
                    job.status = "error"
                    job.error = t.message ?: "download failed"
                }
            } finally {
                // Whatever happened, staging is no longer needed: a finished
                // file has been published, a failed one is junk.
                job.dir.deleteRecursively()
                onActivityChanged()
            }
        }
        onActivityChanged()
        return id
    }

    private fun runDownload(job: MutableJob) {
        val request = YoutubeDLRequest(job.download.url).apply {
            addOption("-o", job.outputTemplate)
            addOption("--newline")
            addOption("--no-playlist")
            addOption("--no-warnings")
            // yt-dlp otherwise stamps the file with the upload date. That buries
            // it in date-sorted file pickers, and it is the mtime desktop's
            // own discovery once tripped over.
            addOption("--no-mtime")
            // Same player-client policy as the desktop app, so both produce
            // the same formats for the same URL. Divergence here would mean
            // the chooser showing options that then fail.
            addOption("--extractor-args", "youtube:player_client=tv_embedded,default,android_vr")

            when (job.download.mp4Mode) {
                "mp3" -> {
                    addOption("-x")
                    addOption("--audio-format", "mp3")
                    // Bitrate arrives in kbps (192), as desktop's preferredquality
                    // takes it. It was divided by 1000 here, which yielded "0" --
                    // yt-dlp's best-VBR setting -- so every choice was ignored.
                    addOption("--audio-quality", "${job.download.bitrate ?: 192}K")
                    // Tags and cover art, as desktop's FFmpegMetadata and
                    // EmbedThumbnail postprocessors add them.
                    addOption("--embed-metadata")
                    addOption("--embed-thumbnail")
                }
                "srt" -> {
                    // Subtitles only: --skip-download with no video format keeps
                    // this to a few KB instead of the whole video.
                    addOption("--skip-download")
                    addOption("--write-subs")
                    addOption("--write-auto-subs")
                    addOption("--sub-langs", job.download.subtitles?.lang ?: "en")
                    addOption("--convert-subs", "srt")
                }
                else -> {
                    addOption("-f", formatSelector(job.download.quality))
                    addOption("--merge-output-format", "mp4")
                    job.download.subtitles?.takeIf { it.embed }?.let { subs ->
                        addOption("--write-subs")
                        addOption("--write-auto-subs")
                        addOption("--sub-langs", subs.lang)
                        addOption("--embed-subs")
                    }
                }
            }
            job.download.trim?.let { trim ->
                // Section cutting rather than post-download trimming: yt-dlp
                // passes the range straight to ffmpeg, so only the requested
                // segment is fetched instead of the whole file.
                addOption("--download-sections", "*${trim.start}-${trim.end}")
            }
        }

        job.status = "downloading"
        // The callback's first argument is a percentage -- the library parses
        // yt-dlp's "37.5%" -- not a fraction. Multiplying it by 100 pinned the
        // bar at 100% from the first tick; found on the emulator.
        val response = YoutubeDL.getInstance().execute(request, job.id) { percent, _, _ ->
            job.progress = percent.toInt().coerceIn(0, 100)
        }

        if (job.status == "cancelled") return
        if (response.exitCode != 0) {
            // The full stderr, for `adb logcat -s MediaGrab`: the job's error
            // is all the user sees, and it is rarely enough to diagnose with.
            Log.w(TAG, "yt-dlp exited ${response.exitCode} for job ${job.id}:\n${response.err}")
            job.status = "error"
            job.error = response.err.trim().ifEmpty { "download failed" }
            return
        }

        // The filename template is generated, so the produced name is found by
        // listing rather than assumed. Assuming produced "samplemp4" once on
        // the desktop build (ARCHITECTURE.md §7.3); do not repeat it.
        val produced = findProducedFile(job)
        if (produced == null) {
            job.status = "error"
            job.error = "yt-dlp finished but produced no file"
            return
        }

        job.stage = "Saving to Downloads"
        val published = try {
            publisher.publish(produced)
        } catch (t: Throwable) {
            job.status = "error"
            job.error = "Could not save to Downloads: ${t.message}"
            return
        }
        job.published = published
        job.filename = published.name
        job.size = formatSize(published.size)
        job.status = "ready"
    }

    /**
     * The job's output. Each job writes into its own directory, so whatever is
     * there belongs to it -- no mtime heuristics. The largest file wins, which
     * skips a stray thumbnail or sidecar subtitle left beside the media.
     */
    private fun findProducedFile(job: MutableJob): File? =
        job.dir.listFiles()
            ?.filter { it.isFile && !it.name.startsWith(".") && !it.name.endsWith(".part") }
            ?.maxByOrNull { it.length() }

    /**
     * Desktop's chain (app/services/downloader.py): H.264 MP4 + M4A first, so
     * the result remuxes and plays everywhere, and only then anything within
     * the height. The bare bestvideo+bestaudio it replaces picked AV1 + Opus
     * on the first emulator run, which many phone players will not open.
     */
    private fun formatSelector(quality: String?): String {
        val h = quality?.removeSuffix("p")?.toIntOrNull() ?: 1080
        return "bestvideo[height<=$h][ext=mp4]+bestaudio[ext=m4a]/" +
            "bestvideo[height<=$h]+bestaudio/" +
            "best[height<=$h][ext=mp4]/" +
            "best[height<=$h]/best"
    }

    override fun progress(jobId: String): JobProgressResponse? =
        jobs[jobId]?.let { job ->
            JobProgressResponse(
                jobId = job.id,
                status = job.status,
                progress = job.progress.toDouble(),
                stage = job.stage,
                filename = job.filename,
                size = job.size,
                error = job.error,
                downloadUrl = if (job.status == "ready") "/api/file/${job.id}" else null,
                queuePosition = 0,
                activeSlots = activeCount(),
            )
        }

    override fun file(jobId: String): StoredFile? {
        val job = jobs[jobId]?.takeIf { it.status == "ready" } ?: return null
        val published = job.published ?: return null
        return StoredFile(
            name = published.name,
            mimeType = mimeTypeFor(published.name),
            size = published.size,
            open = { publisher.open(published.uri) },
        )
    }

    /** Jobs that have not reached a terminal state. */
    fun activeCount(): Int = jobs.values.count { it.status !in TERMINAL }

    /** Progress of the oldest active job, for the notification. */
    fun leadProgress(): Int? = jobs.values.firstOrNull { it.status !in TERMINAL }?.progress

    override fun abort(jobId: String): Boolean {
        val job = jobs[jobId] ?: return false
        if (job.status in TERMINAL) return false
        // Status first, so the download coroutine sees the cancel and does not
        // record the killed process's non-zero exit as an error.
        job.status = "cancelled"
        // destroyProcessById kills the python child process tree, not just the
        // handle, which is what stops the download actually continuing. The
        // coroutine's finally then purges the job's staging directory.
        YoutubeDL.getInstance().destroyProcessById(jobId)
        onActivityChanged()
        return true
    }

    private class MutableJob(
        val id: String,
        val download: DownloadRequest,
        parent: File,
    ) {
        /**
         * One directory per job. The template used to be the URL's last path
         * segment, so every YouTube link became "watch.mp4": a second grab of
         * the same video was skipped as already downloaded and reported no
         * file, and two concurrent jobs would have written the same path.
         */
        val dir = File(parent, id)
        @Volatile var status: String = "queued"
        @Volatile var progress: Int = 0
        @Volatile var filename: String? = null
        @Volatile var size: String? = null
        @Volatile var error: String? = null
        @Volatile var stage: String? = null
        @Volatile var published: DownloadsPublisher.Published? = null

        val outputTemplate: String
            get() = File(dir, "%(title).150B.%(ext)s").absolutePath
    }

    /**
     * Map yt-dlp's exit status onto the HTTP status the frontend expects.
     *
     * The circuit in the Python app uses 451 for an upstream refusal; the
     * frontend routes on it to show the "use the native app" message, so a
     * bot check here must not collapse into a generic 500.
     */
    private fun classifyExit(stderr: String): Int {
        val text = stderr.lowercase()
        return when {
            "sign in to confirm" in text ||
                "confirm you" in text ||
                "bot" in text && "http error" in text -> 451
            "unsupported url" in text -> 400
            else -> 500
        }
    }

}

private val TERMINAL = setOf("ready", "error", "cancelled")
private const val TAG = "MediaGrab"

/** Resolutions offered in the chooser. */
private val STANDARD = listOf(240, 360, 480, 720, 1080, 1440, 2160)

/** Same rendering as desktop's `format_size`, which the ready card displays. */
internal fun formatSize(bytes: Long): String {
    if (bytes <= 0) return "-- MB"
    val mb = bytes / (1024.0 * 1024.0)
    return if (mb >= 10) "${Math.round(mb)} MB" else String.format(java.util.Locale.ROOT, "%.1f MB", mb)
}

/**
 * Map yt-dlp's `-J` output onto [InfoResponse].
 *
 * Mirrors `parse_metadata` in app/services/extractor.py so both servers offer
 * the same choices for the same URL. Kept deliberately defensive: an unknown
 * or missing field is skipped rather than throwing, because a site adding a
 * format should not turn the whole chooser into an error page.
 */
internal fun parseMetadata(raw: String, url: String): InfoResponse {
    val root = Json.parseToJsonElement(raw).jsonObject

    val duration = root["duration"]?.jsonPrimitive?.intOrNull ?: 0

    val audioOnly = root["formats"]?.jsonArray.orEmpty()
        .map { it.jsonObject }
        .filter { (it["vcodec"]?.jsonPrimitive?.content == "none") }
        .filterNot { it["acodec"]?.jsonPrimitive?.content == "none" }
    val bestAudio = audioOnly.maxByOrNull {
        (it["abr"]?.jsonPrimitive?.doubleOrNull
            ?: it["tbr"]?.jsonPrimitive?.doubleOrNull
            ?: 0.0)
    }
    val audioCodec = bestAudio?.get("acodec")?.jsonPrimitive?.content.orEmpty().lowercase()

    val byResolution = LinkedHashMap<Int, QualityOption>()
    for (format in root["formats"]?.jsonArray.orEmpty().map { it.jsonObject }) {
        val height = format["height"]?.jsonPrimitive?.intOrNull ?: continue
        if (height < 240) continue
        val closest = STANDARD.minByOrNull { abs(it - height) } ?: continue
        // Within 15% of a standard rung, or it is a source cut that has no
        // meaning as a "resolution" to pick.
        if (abs(closest - height) > closest * 0.15) continue

        val vcodec = format["vcodec"]?.jsonPrimitive?.content.orEmpty().lowercase()
        val ext = format["ext"]?.jsonPrimitive?.content.orEmpty().lowercase()
        val size = format["filesize"]?.jsonPrimitive?.longOrNullSafe()
            ?: format["filesize_approx"]?.jsonPrimitive?.longOrNullSafe()
            ?: 0L

        val remux = ("avc1" in vcodec || "h264" in vcodec || ext == "mp4") && closest <= 1080

        val existing = byResolution[closest]
        if (existing == null || size > existing.approxBytes) {
            byResolution[closest] = QualityOption(
                resolution = "${closest}p",
                approxBytes = size,
                type = if (remux) "remux" else "encode",
            )
        }
    }

    val subtitles = root["subtitles"]?.jsonObject.orEmpty()
        .plus(root["automatic_captions"]?.jsonObject.orEmpty())
        .map { (lang, entries) ->
            SubtitleOption(
                lang = lang,
                label = lang.uppercase(),
                isAuto = root["automatic_captions"]?.jsonObject?.containsKey(lang) == true,
            )
        }
        .take(12)

    return InfoResponse(
        url = url,
        title = root["title"]?.jsonPrimitive?.content ?: "Untitled",
        uploader = root["uploader"]?.jsonPrimitive?.content
            ?: root["channel"]?.jsonPrimitive?.content
            ?: "Unknown",
        duration = duration,
        thumbnail = root["thumbnail"]?.jsonPrimitive?.content,
        sourceAudioKbps = (bestAudio?.get("abr")?.jsonPrimitive?.doubleOrNull ?: 128.0).toInt(),
        sourceAudioCodec = when {
            "opus" in audioCodec -> "Opus"
            "mp4a" in audioCodec || "aac" in audioCodec -> "AAC"
            audioCodec.isNotEmpty() -> audioCodec
            else -> "opus"
        },
        qualities = byResolution.entries.sortedByDescending { it.key }.map { it.value },
        subtitles = subtitles,
    )
}

private fun JsonPrimitive.longOrNullSafe(): Long? =
    content.toLongOrNull() ?: doubleOrNull?.toLong()