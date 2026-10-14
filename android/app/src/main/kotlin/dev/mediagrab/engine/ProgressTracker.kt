package dev.mediagrab.engine

import java.util.Locale
import kotlin.math.roundToInt
import kotlin.math.roundToLong

/**
 * Turns yt-dlp's stdout, one line at a time, into what the working screen
 * shows -- the same fields, weighting and wording as desktop's progress and
 * postprocessor hooks in app/services/downloader.py.
 *
 * Needed because the library's callback only offers a percentage of the
 * current stream. A video+audio download fetches two streams, each 0..100%,
 * so feeding that through directly filled the bar and then reset it to zero
 * for the audio. One bar spans the streams (video 85% of the download, audio
 * 15%) and sits on the same bands as desktop's app/services/progress.py:
 * 0..4 reading the link, 4..88 downloading, 90..96 processing, 96..100 saving.
 */
class ProgressTracker {

    /** What to apply to the job after a line; null fields are left alone. */
    data class Update(
        val status: String? = null,
        val progress: Int? = null,
        val speed: String? = null,
        val eta: String? = null,
        val got: String? = null,
        val stage: String? = null,
    )

    private var streams = 1
    private var stream = 0

    fun onLine(line: String): Update? {
        FORMATS.find(line)?.let {
            streams = it.groupValues[1].split('+').size.coerceAtLeast(1)
            return null
        }
        if (line.startsWith("[download] Destination:")) {
            stream = (stream + 1).coerceAtMost(streams)
            return null
        }
        PERCENT.find(line)?.let { return downloading(line, it.groupValues[1].toDouble()) }
        return STAGES.entries.firstOrNull { line.startsWith(it.key) }?.let {
            Update(status = "processing", progress = it.value.second, stage = it.value.first)
        }
    }

    private fun downloading(line: String, raw: Double): Update {
        val label: String
        val fraction = when {
            streams >= 2 && stream <= 1 -> { label = "video"; raw / 100 * VIDEO_SHARE }
            streams >= 2 -> { label = "audio"; VIDEO_SHARE + raw / 100 * (1 - VIDEO_SHARE) }
            else -> { label = "stream"; raw / 100 }
        }
        val pct = READ_END + fraction.coerceIn(0.0, 1.0) * (DOWNLOAD_END - READ_END)
        val total = SIZE.find(line)?.let { parseBytes(it.groupValues[1], it.groupValues[2]) }
        val speed = SPEED.find(line)?.let { parseBytes(it.groupValues[1], it.groupValues[2]) }
        val eta = ETA.find(line)?.groupValues?.get(1)?.let(::parseClock)
        val got = if (total != null) {
            "${formatSize((total * raw / 100).roundToLong())} / ${formatSize(total)}"
        } else null
        return Update(
            status = "downloading",
            progress = pct.roundToInt(),
            speed = formatSpeed(speed),
            eta = formatEta(eta),
            got = got,
            stage = "Downloading $label payload",
        )
    }

    companion object {
        const val READ_END = 4.0
        const val DOWNLOAD_END = 88.0
        const val PROCESS_START = 90
        const val TAGS_AT = 94
        const val PROCESS_END = 96
        const val SAVE_END = 99
        private const val VIDEO_SHARE = 0.85

        // [info] abc: Downloading 1 format(s): 398+140
        private val FORMATS = Regex("""Downloading \d+ format\(s\): (\S+)""")
        // [download]  37.5% of ~  67.98MiB at    3.97MiB/s ETA 00:17
        private val PERCENT = Regex("""^\[download]\s+(\d+(?:\.\d+)?)%""")
        private val SIZE = Regex("""of\s+~?\s*(\d+(?:\.\d+)?)([KMGT]?i?B)""")
        private val SPEED = Regex("""at\s+(\d+(?:\.\d+)?)([KMGT]?i?B)/s""")
        private val ETA = Regex("""ETA\s+(\d+(?::\d+)+)""")

        // Desktop's postprocessor_hook wording, keyed on yt-dlp's line prefix.
        private val STAGES = linkedMapOf(
            "[ExtractAudio]" to ("Converting audio to MP3..." to PROCESS_START),
            "[EmbedThumbnail]" to ("Embedding tags & artwork..." to TAGS_AT),
            "[Metadata]" to ("Embedding tags & artwork..." to TAGS_AT),
            "[Merger]" to ("Remuxing video & audio..." to PROCESS_START),
            "[EmbedSubtitle]" to ("Remuxing video & audio..." to PROCESS_START),
            "[FixupM3u8]" to ("Remuxing video & audio..." to PROCESS_START),
        )

        private fun parseBytes(value: String, unit: String): Long {
            val power = when (unit.firstOrNull()) {
                'K' -> 1; 'M' -> 2; 'G' -> 3; 'T' -> 4; else -> 0
            }
            // yt-dlp prints binary units (MiB); a bare "MB" is treated alike.
            var bytes = value.toDouble()
            repeat(power) { bytes *= 1024 }
            return bytes.roundToLong()
        }

        private fun parseClock(clock: String): Long =
            clock.split(':').fold(0L) { acc, part -> acc * 60 + part.toLong() }
    }
}

/** Desktop's `format_speed`. */
internal fun formatSpeed(bytesPerSecond: Long?): String {
    if (bytesPerSecond == null || bytesPerSecond <= 0) return "-- MB/s"
    if (bytesPerSecond >= 1024 * 1024) {
        return String.format(Locale.ROOT, "%.1f MB/s", bytesPerSecond / (1024.0 * 1024.0))
    }
    return String.format(Locale.ROOT, "%.0f KB/s", bytesPerSecond / 1024.0)
}

/** Desktop's `format_eta`. */
internal fun formatEta(seconds: Long?): String {
    if (seconds == null || seconds < 0) return "--:--"
    return String.format(Locale.ROOT, "%d:%02d", seconds / 60, seconds % 60)
}
