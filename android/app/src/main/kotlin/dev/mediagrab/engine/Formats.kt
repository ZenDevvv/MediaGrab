package dev.mediagrab.engine

import dev.mediagrab.contract.QualityOption
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray

/**
 * Which format a video download fetches -- the rule in app/services/formats.py,
 * which documents why. Keep the two in step.
 *
 * The download passes [videoFormatSpec] and [VIDEO_FORMAT_SORT] to yt-dlp.
 * The chooser cannot run yt-dlp's selector in-process the way the Python
 * server does (metadata arrives as `-J` JSON from a separate yt-dlp run), so
 * [offeredQualities] replays the same rule over that JSON: separate video +
 * audio first (highest resolution within the cap, then H.264, then AAC),
 * otherwise a single file, MP4 first.
 */
internal const val VIDEO_FORMAT_SORT = "res,vcodec:h264,acodec:aac"

internal val STANDARD_HEIGHTS = listOf(2160, 1440, 1080, 720, 480, 360, 240, 144)

/** Cap for a label with no number ("Original", a source reporting no heights). */
internal const val UNCAPPED = 100_000

internal fun heightCap(quality: String?): Int {
    if (quality.isNullOrEmpty()) return 1080
    return quality.filter { it.isDigit() }.toIntOrNull() ?: UNCAPPED
}

internal fun videoFormatSpec(quality: String?): String {
    val h = heightCap(quality)
    return "bv[height<=$h]+ba/b[height<=$h][ext=mp4]/b[height<=$h]/b"
}

internal fun ceilingLabel(height: Int): String? =
    STANDARD_HEIGHTS.sorted().firstOrNull { height <= it }?.let { "${it}p" }

private class Fmt(private val json: JsonObject) {
    private fun str(key: String) = (json[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
    private fun num(key: String) = (json[key] as? JsonPrimitive)?.let {
        it.doubleOrNull ?: it.content.trim().toDoubleOrNull()
    }?.takeIf { it > 0 }

    val height = num("height")?.toInt() ?: 0
    val ext = str("ext").orEmpty().lowercase()
    // Absent means unknown, which yt-dlp treats as present.
    val vcodec = str("vcodec")?.lowercase()
    val acodec = str("acodec")?.lowercase()
    val hasVideo = vcodec != "none"
    val hasAudio = acodec != "none"
    val h264 = vcodec != null && ("avc1" in vcodec || "h264" in vcodec)
    val aac = acodec != null && ("mp4a" in acodec || "aac" in acodec)
    val bitrate = num("tbr") ?: ((num("vbr") ?: 0.0) + (num("abr") ?: 0.0))
    private val reported = (num("filesize") ?: num("filesize_approx"))?.toLong()

    fun size(duration: Double): Long =
        reported ?: if (duration > 0) (bitrate * 1000 / 8 * duration).toLong() else 0L
}

private val VIDEO_ORDER = compareBy<Fmt>({ it.height }, { it.h264 }, { it.bitrate })
private val AUDIO_ORDER = compareBy<Fmt>({ it.aac }, { it.bitrate })

private fun pick(formats: List<Fmt>, cap: Int): List<Fmt>? {
    val videoOnly = formats.filter { it.hasVideo && !it.hasAudio && it.height in 1..cap }
    val audioOnly = formats.filter { !it.hasVideo && it.hasAudio }
    if (videoOnly.isNotEmpty() && audioOnly.isNotEmpty()) {
        return listOf(videoOnly.maxWith(VIDEO_ORDER), audioOnly.maxWith(AUDIO_ORDER))
    }
    val single = formats.filter { it.hasVideo && it.hasAudio }
    val withinCap = single.filter { it.height in 1..cap }
    withinCap.filter { it.ext == "mp4" }.maxWithOrNull(VIDEO_ORDER)?.let { return listOf(it) }
    withinCap.maxWithOrNull(VIDEO_ORDER)?.let { return listOf(it) }
    return single.maxWithOrNull(VIDEO_ORDER)?.let { listOf(it) }
}

private fun kind(parts: List<Fmt>): String {
    val video = parts.filter { it.hasVideo }
    return when {
        video.any { it.h264 } -> "remux"
        video.all { it.vcodec == null && it.ext == "mp4" } -> "remux"
        else -> "encode"
    }
}

/**
 * The qualities worth offering: a resolution appears only when the download,
 * asked for it, gets a video whose height rounds up to exactly that label.
 */
internal fun offeredQualities(root: JsonObject): List<QualityOption> {
    val duration = (root["duration"] as? JsonPrimitive)?.doubleOrNull ?: 0.0
    val raw = root["formats"]?.jsonArray?.mapNotNull { it as? JsonObject }
        ?.takeIf { it.isNotEmpty() }
        // A direct file link: the info object is itself the only format.
        ?: listOfNotNull(root.takeIf { it["url"] != null })
    val formats = raw.map(::Fmt)
    if (formats.isEmpty()) return emptyList()

    fun option(label: String, parts: List<Fmt>) =
        QualityOption(resolution = label, approxBytes = parts.sumOf { it.size(duration) }, type = kind(parts))

    if (formats.none { it.height > 0 }) {
        return listOfNotNull(pick(formats, UNCAPPED)?.let { option("Original", it) })
    }
    return STANDARD_HEIGHTS.mapNotNull { h ->
        val label = "${h}p"
        pick(formats, h)?.takeIf { parts -> ceilingLabel(parts.maxOf { it.height }) == label }
            ?.let { option(label, it) }
    }
}
