package dev.mediagrab.engine

import java.util.Locale
import kotlin.math.roundToLong

/**
 * Turns yt-dlp's stdout, one line at a time, into what the working screen
 * shows -- the same fields, weighting and wording as desktop's progress and
 * postprocessor hooks in app/services/downloader.py.
 *
 * Needed because the library's callback only offers a percentage of the
 * current stream. A video+audio download fetches two streams, each 0..100%,
 * so feeding that through directly filled the bar and then reset it to zero
 * for the audio. Desktop weights the streams (video 0..85, audio 85..98, a
 * single stream 0..95) and so does this.
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
            Update(status = "processing", progress = 99, stage = it.value)
        }
    }

    private fun downloading(line: String, raw: Double): Update {
        val label: String
        val pct = when {
            streams >= 2 && stream <= 1 -> { label = "video"; minOf(85.0, raw * 0.85) }
            streams >= 2 -> { label = "audio"; minOf(98.0, 85.0 + raw * 0.13) }
            else -> { label = "stream"; minOf(95.0, raw * 0.95) }
        }
        val total = SIZE.find(line)?.let { parseBytes(it.groupValues[1], it.groupValues[2]) }
        val speed = SPEED.find(line)?.let { parseBytes(it.groupValues[1], it.groupValues[2]) }
        val eta = ETA.find(line)?.groupValues?.get(1)?.let(::parseClock)
        val got = if (total != null) {
            "${formatSize((total * raw / 100).roundToLong())} / ${formatSize(total)}"
        } else null
        return Update(
            status = "downloading",
            progress = pct.toInt(),
            speed = formatSpeed(speed),
            eta = formatEta(eta),
            got = got,
            stage = "Downloading $label payload",
        )
    }

    private companion object {
        // [info] abc: Downloading 1 format(s): 398+140
        val FORMATS = Regex("""Downloading \d+ format\(s\): (\S+)""")
        // [download]  37.5% of ~  67.98MiB at    3.97MiB/s ETA 00:17
        val PERCENT = Regex("""^\[download]\s+(\d+(?:\.\d+)?)%""")
        val SIZE = Regex("""of\s+~?\s*(\d+(?:\.\d+)?)([KMGT]?i?B)""")
        val SPEED = Regex("""at\s+(\d+(?:\.\d+)?)([KMGT]?i?B)/s""")
        val ETA = Regex("""ETA\s+(\d+(?::\d+)+)""")

        // Desktop's postprocessor_hook wording, keyed on yt-dlp's line prefix.
        val STAGES = linkedMapOf(
            "[ExtractAudio]" to "Converting audio to MP3...",
            "[EmbedThumbnail]" to "Embedding tags & artwork...",
            "[Metadata]" to "Embedding tags & artwork...",
            "[Merger]" to "Remuxing video & audio...",
            "[EmbedSubtitle]" to "Remuxing video & audio...",
            "[FixupM3u8]" to "Remuxing video & audio...",
        )

        fun parseBytes(value: String, unit: String): Long {
            val power = when (unit.firstOrNull()) {
                'K' -> 1; 'M' -> 2; 'G' -> 3; 'T' -> 4; else -> 0
            }
            // yt-dlp prints binary units (MiB); a bare "MB" is treated alike.
            var bytes = value.toDouble()
            repeat(power) { bytes *= 1024 }
            return bytes.roundToLong()
        }

        fun parseClock(clock: String): Long =
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
