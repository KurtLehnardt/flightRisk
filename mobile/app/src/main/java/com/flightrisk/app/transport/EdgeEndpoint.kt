package com.flightrisk.app.transport

import com.flightrisk.app.ground.EndpointParseResult
import com.flightrisk.app.ground.GroundEndpoint

/**
 * The ground station's edge address: the same host rules as the dashboard
 * address ([GroundEndpoint]), with the edge port 9000 as the default and
 * `ws://` accepted as a prefix. Encrypted `wss://` is refused because this
 * build's WebSocket client speaks only the plain-text link on the local
 * network; the host must also pass the private-address policy at connect time.
 */
object EdgeEndpoint {
    const val DEFAULT_PORT = 9000

    fun parse(text: String): EndpointParseResult {
        val t = text.trim()
        val scheme = t.substringBefore("://", missingDelimiterValue = "").lowercase()
        if (scheme == "wss" || scheme == "https") {
            return EndpointParseResult.Invalid(
                "This build cannot use an encrypted link (wss://). Use the ground station's local address, for example 192.168.1.20:9000.",
            )
        }
        val withoutScheme = if (scheme == "ws") "http://" + t.substringAfter("://") else t
        val parsed = GroundEndpoint.parseDetailed(withoutScheme)
        if (parsed !is EndpointParseResult.Ok) return parsed
        if (hasExplicitPort(withoutScheme)) return parsed
        return GroundEndpoint.of(parsed.endpoint.host, DEFAULT_PORT)?.let { EndpointParseResult.Ok(it) }
            ?: EndpointParseResult.Invalid("That is not a valid host name or IP address.")
    }

    /** The endpoint, or null when [parse] refuses the text. */
    fun parseOrNull(text: String): GroundEndpoint? = (parse(text) as? EndpointParseResult.Ok)?.endpoint

    private fun hasExplicitPort(text: String): Boolean {
        var t = text.trim()
        val schemeEnd = t.indexOf("://")
        if (schemeEnd >= 0) t = t.substring(schemeEnd + 3)
        t = t.removeSuffix("/")
        return if (t.startsWith("[")) t.contains("]:") else t.count { it == ':' } == 1
    }
}
