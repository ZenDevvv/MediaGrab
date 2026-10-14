package dev.mediagrab

/**
 * The link inside text shared to the app.
 *
 * Apps rarely share a bare URL: YouTube sends "Title https://youtu.be/abc",
 * others add a sentence around it. The first http(s) URL is taken, with
 * trailing punctuation a sentence would leave stripped off. Kept free of
 * Android types so it is testable on the JVM.
 */
fun extractSharedUrl(text: String?): String? {
    if (text.isNullOrBlank()) return null
    val match = URL.find(text) ?: return null
    return match.value.trimEnd('.', ',', ';', ':', '!', '?', ')', ']', '"', '\'')
}

private val URL = Regex("""https?://[^\s<>"]+""", RegexOption.IGNORE_CASE)
