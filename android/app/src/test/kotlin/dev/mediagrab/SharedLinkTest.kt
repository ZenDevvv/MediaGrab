package dev.mediagrab

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SharedLinkTest {
    @Test
    fun `takes the link out of a titled share`() {
        assertEquals(
            "https://youtu.be/jNQXAC9IVRw?si=abc",
            extractSharedUrl("Me at the zoo https://youtu.be/jNQXAC9IVRw?si=abc"),
        )
    }

    @Test
    fun `a bare link is returned as is`() {
        assertEquals("https://vimeo.com/76979871", extractSharedUrl("https://vimeo.com/76979871"))
    }

    @Test
    fun `strips punctuation a sentence leaves behind`() {
        assertEquals(
            "https://example.com/v/1",
            extractSharedUrl("Watch this (https://example.com/v/1)."),
        )
    }

    @Test
    fun `the first of several links wins`() {
        assertEquals("http://a.example/x", extractSharedUrl("http://a.example/x and https://b.example/y"))
    }

    @Test
    fun `text without a link is ignored`() {
        assertNull(extractSharedUrl("no link here"))
        assertNull(extractSharedUrl(null))
        assertNull(extractSharedUrl("   "))
    }
}
