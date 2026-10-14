package dev.mediagrab

import dev.mediagrab.engine.ProgressTracker
import dev.mediagrab.engine.formatEta
import dev.mediagrab.engine.formatSpeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Lines are yt-dlp's real output, captured on this project's own runs. Expected
 * strings match what desktop's format_* helpers produce for the same values.
 */
class ProgressTrackerTest {

    private fun video(t: ProgressTracker) {
        t.onLine("[info] aqz-KE-bpKQ: Downloading 1 format(s): 398+140")
        t.onLine("[download] Destination: /x/Big Buck Bunny.f398.mp4")
    }

    @Test
    fun `reports speed, eta and amount as desktop formats them`() {
        val t = ProgressTracker()
        video(t)
        val u = t.onLine("[download]  37.5% of   67.98MiB at    3.97MiB/s ETA 00:17")!!
        assertEquals("downloading", u.status)
        assertEquals(31, u.progress) // 37.5 * 0.85, as desktop weights video
        assertEquals("4.0 MB/s", u.speed)
        assertEquals("0:17", u.eta)
        assertEquals("25 MB / 68 MB", u.got)
        assertEquals("Downloading video payload", u.stage)
    }

    @Test
    fun `the bar does not reset when the audio stream starts`() {
        // The bug this exists for: each stream runs 0..100, so the raw
        // percentage fell from 100 back to 0 between video and audio.
        val t = ProgressTracker()
        video(t)
        val endOfVideo = t.onLine("[download] 100% of   67.98MiB in 00:00:17 at 3.9MiB/s")!!.progress!!
        t.onLine("[download] Destination: /x/Big Buck Bunny.f140.m4a")
        val startOfAudio = t.onLine("[download]   0.0% of    9.80MiB at  Unknown B/s ETA Unknown")!!
        assertTrue("$endOfVideo then ${startOfAudio.progress}", startOfAudio.progress!! >= endOfVideo)
        assertEquals(85, startOfAudio.progress)
        assertEquals("Downloading audio payload", startOfAudio.stage)
        // Unknown speed and ETA render as desktop's placeholders.
        assertEquals("-- MB/s", startOfAudio.speed)
        assertEquals("--:--", startOfAudio.eta)
        assertEquals(98, t.onLine("[download] 100% of    9.80MiB in 00:00:02 at 4.1MiB/s")!!.progress)
    }

    @Test
    fun `a single stream is weighted to 95`() {
        val t = ProgressTracker()
        t.onLine("[info] x: Downloading 1 format(s): 140")
        t.onLine("[download] Destination: /x/a.m4a")
        assertEquals(95, t.onLine("[download] 100% of    9.80MiB in 00:00:02 at 4.1MiB/s")!!.progress)
        assertEquals("Downloading stream payload",
            t.onLine("[download]  50.0% of ~   9.80MiB at  512.00KiB/s ETA 00:10")!!.stage)
    }

    @Test
    fun `postprocessing lines become desktop's stages`() {
        val t = ProgressTracker()
        val merge = t.onLine("[Merger] Merging formats into \"/x/Big Buck Bunny.mp4\"")!!
        assertEquals("processing", merge.status)
        assertEquals(99, merge.progress)
        assertEquals("Remuxing video & audio...", merge.stage)
        assertEquals("Converting audio to MP3...", t.onLine("[ExtractAudio] Destination: /x/a.mp3")!!.stage)
        assertEquals("Embedding tags & artwork...", t.onLine("[EmbedThumbnail] ffmpeg: Adding thumbnail")!!.stage)
    }

    @Test
    fun `unrelated lines change nothing`() {
        val t = ProgressTracker()
        assertNull(t.onLine("[youtube] Extracting URL: https://www.youtube.com/watch?v=x"))
        assertNull(t.onLine(""))
    }

    @Test
    fun `formatters match desktop`() {
        assertEquals("512 KB/s", formatSpeed(512 * 1024))
        assertEquals("1.5 MB/s", formatSpeed(1_572_864))
        assertEquals("-- MB/s", formatSpeed(0))
        assertEquals("1:05", formatEta(65))
        assertEquals("--:--", formatEta(null))
    }
}
