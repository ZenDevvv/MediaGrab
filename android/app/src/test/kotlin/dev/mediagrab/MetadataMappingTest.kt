package dev.mediagrab

import dev.mediagrab.engine.VIDEO_FORMAT_SORT
import dev.mediagrab.engine.parseMetadata
import dev.mediagrab.engine.videoFormatSpec
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * yt-dlp's `-J` payload mapped onto the wire types.
 *
 * This is the one part of the engine that can be verified without a device —
 * it is pure JSON in, pure DTO out. Everything that touches the embedded
 * Python needs real hardware; this does not.
 *
 * The expectations come from a real yt-dlp `--dump-json` response, because
 * hand-written fixtures drift from what the tool actually emits.
 */
class MetadataMappingTest {

    /** Shared with tests/test_formats.py, which asserts the same qualities. */
    private val payload = File(System.getProperty("mediagrab.fixtures"), "formats-mixed.json").readText()

    @Test
    fun `maps the scalar fields`() {
        val info = parseMetadata(payload, "https://example.invalid/watch?v=x")
        assertEquals("Big Buck Bunny", info.title)
        assertEquals("Blender", info.uploader)
        assertEquals(596, info.duration)
        assertEquals("https://i.example.invalid/hq.jpg", info.thumbnail)
        assertEquals("https://example.invalid/watch?v=x", info.url)
    }

    @Test
    fun `offers what the download rule actually fetches`() {
        // Same expectation as MIXED_EXPECTED in tests/test_formats.py, where
        // yt-dlp's real selector runs over this file. 1080p is the H.264
        // video-only stream plus AAC, not the larger muxed file; a 720p
        // request would get the 360p stream, so 720p is not offered at all.
        val offered = parseMetadata(payload, "u").qualities
            .map { Triple(it.resolution, it.approxBytes, it.type) }
        assertEquals(
            listOf(
                Triple("1080p", 120_000_000L + 9_536_000L, "remux"),
                Triple("360p", 8_000_000L + 9_536_000L, "encode"),
                Triple("144p", 500_000L + 9_536_000L, "remux"),
            ),
            offered,
        )
    }

    @Test
    fun `a low resolution source offers only what exists`() {
        // "Me at the zoo": nothing above 240p, and AV1 inside .mp4 beside H.264.
        val zoo = """
        {"duration": 19, "formats": [
          {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "filesize": 309288, "abr": 129},
          {"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm", "filesize": 252182, "abr": 106},
          {"format_id": "160", "height": 144, "vcodec": "avc1.4D400B", "acodec": "none", "ext": "mp4", "filesize": 195278},
          {"format_id": "133", "height": 240, "vcodec": "avc1.4D400C", "acodec": "none", "ext": "mp4", "filesize": 433081},
          {"format_id": "395", "height": 240, "vcodec": "av01.0.00M.08", "acodec": "none", "ext": "mp4", "filesize": 223779}
        ]}
        """.trimIndent()
        val offered = parseMetadata(zoo, "u").qualities
        assertEquals(listOf("240p", "144p"), offered.map { it.resolution })
        assertEquals(433_081L + 309_288L, offered.first().approxBytes)
    }

    @Test
    fun `the download asks yt-dlp for the same rule`() {
        assertEquals("bv[height<=720]+ba/b[height<=720][ext=mp4]/b[height<=720]/b", videoFormatSpec("720p"))
        assertEquals("res,vcodec:h264,acodec:aac", VIDEO_FORMAT_SORT)
    }

    @Test
    fun `marks h264 and mp4 up to 1080 as remux`() {
        val info = parseMetadata(payload, "u")
        assertEquals("remux", info.qualities.first { it.resolution == "1080p" }.type)
        // VP9 in webm needs a transcode.
        assertEquals("encode", info.qualities.first { it.resolution == "360p" }.type)
    }

    @Test
    fun `reports the best audio stream`() {
        val info = parseMetadata(payload, "u")
        assertEquals(160, info.sourceAudioKbps)
        assertEquals("Opus", info.sourceAudioCodec)
    }

    @Test
    fun `merges manual and automatic subtitles and marks the automatic ones`() {
        val subs = parseMetadata(payload, "u").subtitles.associateBy { it.lang }
        assertEquals(2, subs.size)
        assertFalse("en was listed manually", subs.getValue("en").isAuto)
        assertTrue("fr came from automatic_captions", subs.getValue("fr").isAuto)
    }

    @Test
    fun `survives a payload with nothing optional in it`() {
        // Sites add and remove fields constantly. A missing format list or a
        // null duration must not turn the chooser into an error page.
        val sparse = parseMetadata("""{"title":"Bare"}""", "u")
        assertEquals("Bare", sparse.title)
        assertEquals("Unknown", sparse.uploader)
        assertEquals(0, sparse.duration)
        assertTrue(sparse.qualities.isEmpty())
        assertTrue(sparse.subtitles.isEmpty())
    }

    @Test
    fun `falls back to channel when uploader is absent`() {
        val info = parseMetadata("""{"title":"t","channel":"Chan"}""", "u")
        assertEquals("Chan", info.uploader)
    }

    @Test
    fun `emits only snake_case so the shared frontend can read it`() {
        // The failure this prevents: Kotlin defaulting to camelCase, the
        // frontend reading .source_audio_kbps, getting undefined, and showing
        // nothing at all with no error to trace it from.
        val json = dev.mediagrab.contract.Probe.info(parseMetadata(payload, "u"))
        assertFalse(json.contains("\"sourceAudio"))
        assertFalse(json.contains("\"approxBytes"))
        assertFalse(json.contains("\"isAuto"))
        assertTrue(json.contains("\"source_audio_kbps\""))
        assertTrue(json.contains("\"approx_bytes\""))
    }
}