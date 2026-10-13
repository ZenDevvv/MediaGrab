package dev.mediagrab

import dev.mediagrab.engine.formatSize
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The ready card shows this string verbatim, so it must render as desktop's
 * `format_size` does. Android once sent raw bytes ("475957").
 */
class FormatSizeTest {
    @Test
    fun `matches the desktop rendering`() {
        assertEquals("-- MB", formatSize(0))
        assertEquals("0.5 MB", formatSize(475_957))
        assertEquals("9.5 MB", formatSize(10_000_000))
        assertEquals("48 MB", formatSize(50_000_000))
    }
}
