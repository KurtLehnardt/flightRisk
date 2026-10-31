package com.flightrisk.app.ground

import java.io.IOException
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress

/**
 * Where the dashboard runs: a host and a port, reached over plain `http://`
 * and `ws://` on the fleet's private network. Holds no credentials.
 */
class GroundEndpoint private constructor(
    /** The host as entered: a hostname, an IPv4 literal or an IPv6 literal without brackets. */
    val host: String,
    val port: Int,
) {
    private val isIpv6Literal: Boolean get() = host.contains(':')

    /** The value of the HTTP `Host` header (`host:port`, IPv6 in brackets). */
    val hostHeader: String get() = (if (isIpv6Literal) "[$host]" else host) + ":" + port

    /** The dashboard page, for "open in the browser". */
    val httpBase: String get() = "http://$hostHeader"

    override fun equals(other: Any?): Boolean = other is GroundEndpoint && other.host == host && other.port == port
    override fun hashCode(): Int = host.hashCode() * 31 + port
    override fun toString(): String = httpBase

    companion object {
        /** The dashboard's default port. */
        const val DEFAULT_PORT = 5555

        /** The Engine.IO 4 WebSocket path; the phone never uses long-polling. */
        const val SOCKET_IO_PATH = "/socket.io/?EIO=4&transport=websocket"

        private val HOSTNAME = Regex("^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\\.?$")
        private val IPV6 = Regex("^[0-9A-Fa-f:]{2,39}$")

        /**
         * An endpoint for [host] and [port], or null when the host is not a
         * plain hostname or IP literal or the port is out of range. No
         * lookup happens here.
         */
        fun of(host: String, port: Int = DEFAULT_PORT): GroundEndpoint? {
            if (port !in 1..65535) return null
            var h = host.trim()
            if (h.startsWith("[") && h.endsWith("]")) h = h.substring(1, h.length - 1)
            if (h.isEmpty()) return null
            val valid = if (h.contains(':')) isIpv6Literal(h) else HOSTNAME.matches(h)
            return if (valid) GroundEndpoint(h.removeSuffix(".").ifEmpty { return null }, port) else null
        }

        /** A plain IPv6 literal: at most one `::`, at most eight groups of one to four hex digits. */
        private fun isIpv6Literal(h: String): Boolean {
            if (!IPV6.matches(h) || h.contains('.')) return false
            val doubleColons = h.windowed(2).count { it == "::" }
            if (doubleColons > 1 || h.contains(":::")) return false
            val groups = h.split(':')
            val nonEmpty = groups.filter { it.isNotEmpty() }
            if (nonEmpty.any { it.length > 4 }) return false
            return if (doubleColons == 1) nonEmpty.size <= 7 else groups.size == 8 && nonEmpty.size == 8
        }

        /**
         * Parses what a user types: `host`, `host:port`, `[v6]:port` or
         * `http://host:port/`, or null. [parseDetailed] says why a text was refused.
         */
        fun parse(text: String): GroundEndpoint? = (parseDetailed(text) as? EndpointParseResult.Ok)?.endpoint

        /**
         * Like [parse], with a reason the UI can show when the text is refused.
         * Refused: any scheme but `http` (the dashboard serves plain HTTP on
         * the local network; there is no TLS to use), a path, query, fragment
         * or user info, a port outside 1..65535, and an IPv6 zone id (`%`):
         * a link-local `fe80::` address needs one on Android, so link-local
         * IPv6 cannot be used; use the ground's IPv4 or a ULA address.
         */
        fun parseDetailed(text: String): EndpointParseResult {
            var t = text.trim()
            if (t.isEmpty()) return EndpointParseResult.Invalid("Enter the ground station's address.")
            val schemeEnd = t.indexOf("://")
            if (schemeEnd >= 0) {
                val scheme = t.substring(0, schemeEnd).lowercase()
                if (scheme != "http") {
                    return EndpointParseResult.Invalid(
                        if (scheme == "https" || scheme == "wss") {
                            "The ground station serves plain http:// on the local network; https and wss are not supported."
                        } else {
                            "Only http:// addresses are supported."
                        },
                    )
                }
                t = t.substring(schemeEnd + 3)
            }
            t = t.removeSuffix("/")
            if (t.contains('%')) {
                return EndpointParseResult.Invalid("IPv6 zone ids (%) are not supported; use the ground station's IPv4 address.")
            }
            if (t.isEmpty() || t.any { it == '/' || it == '?' || it == '#' || it == '@' || it.isWhitespace() }) {
                return EndpointParseResult.Invalid("Enter only the host and port, for example 192.168.1.20:5555.")
            }
            val hostAndPort: Pair<String, String?> = if (t.startsWith("[")) {
                val close = t.indexOf(']')
                if (close < 0) return EndpointParseResult.Invalid("The IPv6 address is missing its closing bracket.")
                val rest = t.substring(close + 1)
                when {
                    rest.isEmpty() -> t.substring(1, close) to null
                    rest.startsWith(":") -> t.substring(1, close) to rest.substring(1)
                    else -> return EndpointParseResult.Invalid("Enter only the host and port, for example 192.168.1.20:5555.")
                }
            } else {
                when (t.count { it == ':' }) {
                    0 -> t to null
                    1 -> t.substringBefore(':') to t.substringAfter(':')
                    else -> t to null // a bare IPv6 literal
                }
            }
            val port = hostAndPort.second?.let {
                it.toIntOrNull()?.takeIf { p -> p in 1..65535 }
                    ?: return EndpointParseResult.Invalid("The port must be a number from 1 to 65535.")
            } ?: DEFAULT_PORT
            return of(hostAndPort.first, port)?.let { EndpointParseResult.Ok(it) }
                ?: EndpointParseResult.Invalid("That is not a valid host name or IP address.")
        }
    }
}

/** The result of [GroundEndpoint.parseDetailed]. */
sealed interface EndpointParseResult {
    data class Ok(val endpoint: GroundEndpoint) : EndpointParseResult

    /** [reason] is a sentence for the user. */
    data class Invalid(val reason: String) : EndpointParseResult
}

/**
 * The dashboard API key. It is sent only in the Socket.IO connect `auth`
 * object and in the `Authorization: Bearer` header of REST calls, and its
 * [toString] never shows it.
 */
class DashboardApiKey private constructor(internal val secret: String) {
    override fun equals(other: Any?): Boolean = other is DashboardApiKey && other.secret == secret
    override fun hashCode(): Int = secret.hashCode()
    override fun toString(): String = "DashboardApiKey(${Redact.PLACEHOLDER})"

    companion object {
        /** The longest key accepted. */
        const val MAX_LENGTH = 512

        /**
         * A key from [raw], or null when it is empty, too long, or contains
         * a character outside printable ASCII (a space, a line break or a
         * control character could otherwise end up in a header).
         */
        fun of(raw: String?): DashboardApiKey? {
            val k = raw?.trim() ?: return null
            if (k.isEmpty() || k.length > MAX_LENGTH) return null
            if (k.any { it.code < 0x21 || it.code > 0x7e }) return null
            return DashboardApiKey(k)
        }
    }
}

/** Resolves a hostname to its addresses. Tests replace it; the default uses the system resolver. */
fun interface HostResolver {
    @Throws(IOException::class)
    fun resolve(host: String): List<InetAddress>

    companion object {
        val SYSTEM: HostResolver = HostResolver { host -> InetAddress.getAllByName(host).toList() }
    }
}

/** The host resolved to an address outside the private ranges, so nothing was sent. */
class AddressNotAllowedException(message: String) : IOException(message)

/**
 * Cleartext goes only to private, link-local and loopback addresses
 * (docs/mobile-contract.md, "Cleartext on the local network").
 *
 * Android's network security config cannot express address ranges, and a
 * raw socket does not consult it anyway, so the ground clients enforce the
 * rule here, on the resolved address, before they open a connection. Every
 * address a name resolves to must be allowed, and the connection then goes
 * to that checked address, so a second lookup cannot swap in a public one.
 */
object LanAddressPolicy {
    /** True for 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fe80::/10 and fc00::/7. */
    fun isAllowed(address: InetAddress): Boolean {
        val b = address.address
        return when (address) {
            is Inet4Address -> {
                val b0 = b[0].toInt() and 0xff
                val b1 = b[1].toInt() and 0xff
                b0 == 127 || b0 == 10 || (b0 == 172 && b1 in 16..31) || (b0 == 192 && b1 == 168) ||
                    (b0 == 169 && b1 == 254)
            }
            is Inet6Address -> {
                val b0 = b[0].toInt() and 0xff
                val b1 = b[1].toInt() and 0xff
                address.isLoopbackAddress || (b0 == 0xfe && (b1 and 0xc0) == 0x80) || (b0 and 0xfe) == 0xfc
            }
            else -> false
        }
    }

    /**
     * Resolves [host] and returns the address to connect to.
     *
     * @throws AddressNotAllowedException when any resolved address is not private.
     * @throws IOException when the name does not resolve.
     */
    fun resolve(host: String, resolver: HostResolver): InetAddress {
        val addresses = resolver.resolve(host)
        if (addresses.isEmpty()) throw IOException("the host name did not resolve")
        val refused = addresses.firstOrNull { !isAllowed(it) }
        if (refused != null) {
            throw AddressNotAllowedException(
                "the ground station must be on a private network (10.x, 172.16-31.x, 192.168.x, " +
                    "link-local or loopback); ${Redact.truncate(host, 80)} resolved to ${refused.hostAddress}",
            )
        }
        return addresses.first()
    }
}
