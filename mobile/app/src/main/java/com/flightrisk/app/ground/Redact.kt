package com.flightrisk.app.ground

/**
 * Removes secrets from text before it is logged, shown or put in an exception.
 *
 * The order is fixed: every secret is redacted first, and only then is the
 * text truncated. Truncating first could cut a key in half and leave its
 * first characters visible, which the redaction step would then no longer
 * recognise.
 */
object Redact {
    /** What a removed secret is replaced with. */
    const val PLACEHOLDER = "<redacted>"

    /** The longest text this helper returns by default, in UTF-16 characters. */
    const val MAX_LENGTH = 200

    /** Appended when the text was cut. */
    const val ELLIPSIS = "…"

    // An Authorization header value, wherever it appears.
    private val BEARER = Regex("(?i)\\bBearer\\s+[^\\s\"',;]+")

    // A JSON or form field that carries a credential: "api_key":"...", "token": "...".
    private val CREDENTIAL_FIELD = Regex(
        "(?i)(\"?(?:api_key|apikey|token|password|secret|authorization)\"?\\s*[:=]\\s*\"?)([^\"\\s,}&]+)",
    )

    // Line breaks and other control characters would let a server forge log lines.
    private val CONTROL = Regex("[\\u0000-\\u001f\\u007f]")

    /**
     * Returns [text] with every non-empty value in [secrets] replaced by
     * [PLACEHOLDER], with Bearer values and credential fields removed, with
     * control characters replaced by spaces, and then cut to at most
     * [maxLength] characters (the last of which is [ELLIPSIS] when cut).
     */
    fun text(text: String?, secrets: Iterable<String?> = emptyList(), maxLength: Int = MAX_LENGTH): String {
        if (text.isNullOrEmpty()) return ""
        var out: String = text
        // Longest first, so a secret that contains another is removed whole.
        for (secret in secrets.filterNotNull().filter { it.isNotEmpty() }.distinct().sortedByDescending { it.length }) {
            out = out.replace(secret, PLACEHOLDER)
        }
        out = BEARER.replace(out, "Bearer $PLACEHOLDER")
        out = CREDENTIAL_FIELD.replace(out) { m -> m.groupValues[1] + PLACEHOLDER }
        out = CONTROL.replace(out, " ")
        return truncate(out, maxLength)
    }

    /** Cuts [text] to at most [maxLength] characters without splitting a surrogate pair. */
    fun truncate(text: String, maxLength: Int = MAX_LENGTH): String {
        require(maxLength >= 1) { "maxLength must be at least 1" }
        if (text.length <= maxLength) return text
        var cut = maxLength - ELLIPSIS.length
        if (cut > 0 && Character.isHighSurrogate(text[cut - 1])) cut--
        return text.substring(0, cut.coerceAtLeast(0)) + ELLIPSIS
    }
}
