package dev.mediagrab

import dev.mediagrab.engine.parseMetadata
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

    private val payload = """
    {
      "title": "Big Buck Bunny",
      "uploader": "Blender",
      "channel": "Blender",
      "duration": 596,
      "thumbnail": "https://i.example.invalid/hq.jpg",
      "subtitles": { "en": [{"ext": "vtt"}] },
      "automatic_captions": { "fr": [{"ext": "vtt"}] },
      "formats": [
        {"height": 1080, "vcodec": "avc1.640028", "acodec": "none", "ext": "mp4",
         "filesize": 120000000, "abr": 128.0, "tbr": 3000.0},
        {"height": 1080, "vcodec": "avc1.640028", "acodec": "mp4a.40.2", "ext": "mp4",
         "filesize": 150000000, "abr": 192.0, "tbr": 3200.0},
        {"height": 720, "vcodec": "avc1.4d401f", "acodec": "mp4a.40.2", "ext": "mp4",
         "filesize": 50000000, "abr": 128.0, "tbr": 1500.0},
        {"height": 360, "vcodec": "vp9", "acodec": "none", "ext": "webm",
         "filesize": 8000000, "abr": 96.0},
        {"height": 144, "vcodec": "avc1", "acodec": "none", "ext": "mp4",
         "filesize": 500000, "abr": 64.0},
        {"vcodec": "none", "acodec": "opus", "abr": 160.0},
        {"vcodec": "none", "acodec": "mp4a.40.2", "abr": 128.0}
      ]
    }
    """.trimIndent()

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
    fun `keeps only resolutions close to a standard rung`() {
        // 144 is below the 240 floor and must not appear; the chooser has no
        // rung for it and offering it produces a selection that cannot be
        // satisfied.
        val resolutions = parseMetadata(payload, "u").qualities.map { it.resolution }
        assertEquals(listOf("1080p", "720p", "360p"), resolutions)
    }

    @Test
    fun `keeps the largest file when a resolution appears twice`() {
        val info = parseMetadata(payload, "u")
        val hd = info.qualities.first { it.resolution == "1080p" }
        assertEquals(150_000_000L, hd.approxBytes)
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