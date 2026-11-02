package com.flightrisk.app.ground

/**
 * A single-pass structure check run on untrusted JSON text before it is parsed.
 *
 * kotlinx.serialization reads nested arrays recursively, so a 10 KB frame of
 * `[[[[...]]]]` overflows a thread stack, and an 8 MiB array of small
 * numbers becomes hundreds of megabytes of element objects. This scan counts
 * nesting depth and values without allocating anything, skipping the contents
 * of strings (escapes included), and refuses the text before the parser sees
 * it. It does not validate JSON; the parser still does that.
 */
object JsonGuard {
    /** The deepest nesting accepted. The deepest dashboard payload nests about eight levels. */
    const val MAX_DEPTH = 32

    /** The most values (containers, members and array elements) accepted in one text. */
    const val MAX_VALUES = 100_000

    /** Returns null when [text] is within the limits, or the reason it is not. */
    fun check(text: CharSequence, maxDepth: Int = MAX_DEPTH, maxValues: Int = MAX_VALUES): String? {
        var depth = 0
        var values = 0
        var inString = false
        var escaped = false
        for (i in 0 until text.length) {
            val c = text[i]
            if (inString) {
                when {
                    escaped -> escaped = false
                    c == '\\' -> escaped = true
                    c == '"' -> inString = false
                }
                continue
            }
            when (c) {
                '"' -> inString = true
                '[', '{' -> {
                    depth++
                    if (depth > maxDepth) return "JSON nested deeper than $maxDepth levels"
                    if (++values > maxValues) return "JSON with more than $maxValues values"
                }
                ']', '}' -> depth--
                ',' -> if (++values > maxValues) return "JSON with more than $maxValues values"
            }
        }
        return null
    }
}
