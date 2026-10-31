package com.flightrisk.app.ground

import com.flightrisk.contract.parseJsonOrNull
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.net.InetSocketAddress
import java.net.Socket

/** The outcome of a REST health probe ("Test connection"). Every text is redacted. */
sealed interface HealthProbeResult {
    /** The ground answered `/api/health` and accepted the key on `/api/status`. */
    data class Ok(val version: String?, val summary: StatusSummary) : HealthProbeResult

    /** The ground answered, but `/api/status` said 401: a wrong or missing key. */
    data class WrongKey(val version: String?) : HealthProbeResult

    /** The ground answered with a redirect, which is never followed. */
    data class Redirected(val path: String, val status: Int) : HealthProbeResult

    /** The ground answered with another unexpected HTTP status. */
    data class UnexpectedStatus(val path: String, val status: Int) : HealthProbeResult

    /** The answer was not the JSON the dashboard sends (wrong type, not an object, missing keys, too large). */
    data class UnexpectedBody(val path: String, val detail: String) : HealthProbeResult

    /** The host is not on a private network, so nothing was sent. */
    data class AddressNotAllowed(val detail: String) : HealthProbeResult

    /** The ground could not be reached. */
    data class Unreachable(val detail: String) : HealthProbeResult
}

/** The few `/api/status` fields the phone shows. */
data class StatusSummary(val running: Boolean?, val source: String?, val fleetDrones: Int?)

/**
 * The dashboard's REST endpoints the phone uses: `GET /api/health` (public)
 * and `GET /api/status` (needs `Authorization: Bearer <key>`).
 *
 * Plain HTTP/1.1 over a socket to an address [LanAddressPolicy] has checked,
 * like [RawWebSocket] and for the same reason: Android's network security
 * config cannot allow cleartext for address ranges only, and keeping it closed
 * for every other HTTP stack in the app is worth more than reusing OkHttp for
 * two GET requests. Redirects are never followed, bodies are capped at
 * [maxBodyBytes], the answer must be a JSON object of the expected shape, and
 * the key is sent only to `/api/status`, only in the Bearer header.
 */
class DashboardRestClient(
    private val endpoint: GroundEndpoint,
    private val apiKey: DashboardApiKey?,
    private val timeoutMs: Int = 5_000,
    private val maxBodyBytes: Int = 256 * 1024,
    private val resolver: HostResolver = HostResolver.SYSTEM,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val guard: IoGuard = IoGuard.NONE,
) {
    private class Response(val status: Int, val contentType: String?, val body: ByteArray)

    /**
     * `/api/health` then `/api/status`. Safe to call from any thread (all
     * socket IO runs on [ioDispatcher]); cancelling the caller closes the
     * socket at once. Never throws except for cancellation.
     */
    suspend fun probe(): HealthProbeResult {
        var path = HEALTH
        return try {
            val health = get(HEALTH, withKey = false)
            val version = when (val checked = checkHealth(health)) {
                is HealthCheck.Bad -> return checked.result
                is HealthCheck.Good -> checked.version
            }
            path = STATUS
            val status = get(STATUS, withKey = true)
            checkStatus(status, version)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            // An Error (a stack overflow from a hostile body, for example) is a failed probe, not a crash.
            failure(path, e)
        }
    }

    private fun checkStatus(status: Response, version: String?): HealthProbeResult {
        return when (status.status) {
            200 -> {
                val o = jsonObject(status) ?: return HealthProbeResult.UnexpectedBody(STATUS, "not a JSON object")
                val running = o["running"] as? JsonPrimitive
                if (running == null || running.isString || running.content.toBooleanStrictOrNull() == null ||
                    !o.containsKey("source")
                ) {
                    return HealthProbeResult.UnexpectedBody(STATUS, "not the dashboard's status object")
                }
                val fleet = o["fleet"] as? JsonObject
                HealthProbeResult.Ok(
                    version,
                    StatusSummary(
                        running = running.content.toBooleanStrictOrNull(),
                        source = (o["source"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.let { Redact.text(it) },
                        fleetDrones = (fleet?.get("drones") as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toIntOrNull(),
                    ),
                )
            }
            401, 403 -> HealthProbeResult.WrongKey(version)
            in 300..399 -> HealthProbeResult.Redirected(STATUS, status.status)
            else -> HealthProbeResult.UnexpectedStatus(STATUS, status.status)
        }
    }

    private sealed interface HealthCheck {
        data class Good(val version: String?) : HealthCheck
        data class Bad(val result: HealthProbeResult) : HealthCheck
    }

    private fun checkHealth(r: Response): HealthCheck {
        if (r.status in 300..399) return HealthCheck.Bad(HealthProbeResult.Redirected(HEALTH, r.status))
        if (r.status != 200) return HealthCheck.Bad(HealthProbeResult.UnexpectedStatus(HEALTH, r.status))
        val o = jsonObject(r) ?: return HealthCheck.Bad(HealthProbeResult.UnexpectedBody(HEALTH, "not a JSON object"))
        val status = (o["status"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (status == null || o["components"] !is JsonObject) {
            return HealthCheck.Bad(HealthProbeResult.UnexpectedBody(HEALTH, "not the dashboard's health object"))
        }
        val version = (o["version"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.let { Redact.text(it, maxLength = 40) }
        return HealthCheck.Good(version)
    }

    private fun jsonObject(r: Response): JsonObject? {
        val type = r.contentType?.substringBefore(';')?.trim()
        if (!type.equals("application/json", ignoreCase = true)) return null
        val text = String(r.body, Charsets.UTF_8)
        // Checked before parsing: deep nesting would overflow the parser's stack.
        JsonGuard.check(text)?.let { throw ProtocolViolationException(it) }
        return parseJsonOrNull(text) as? JsonObject
    }

    private fun failure(path: String, e: Throwable): HealthProbeResult {
        val secrets = listOfNotNull(apiKey?.secret)
        val detail = Redact.text(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: ""), secrets)
        return when (e) {
            is AddressNotAllowedException -> HealthProbeResult.AddressNotAllowed(detail)
            is MessageTooLargeException -> HealthProbeResult.UnexpectedBody(path, "the answer is larger than $maxBodyBytes bytes")
            is ProtocolViolationException, is Error -> HealthProbeResult.UnexpectedBody(path, detail)
            else -> HealthProbeResult.Unreachable(detail)
        }
    }

    /** One GET under one overall deadline of [timeoutMs] for connect, request and the whole answer. */
    private suspend fun get(path: String, withKey: Boolean): Response {
        val socket = Socket()
        return cancellableSocketIo(socket, ioDispatcher) { socket.use { getBlocking(it, path, withKey) } }
    }

    private fun getBlocking(socket: Socket, path: String, withKey: Boolean): Response {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        guard.beforeBlockingIo("resolve")
        val address = LanAddressPolicy.resolve(endpoint.host, resolver)
        guard.beforeBlockingIo("connect")
        socket.connect(InetSocketAddress(address, endpoint.port), timeoutMs)
        run {
            val input = BufferedInputStream(
                DeadlineInputStream(socket, socket.getInputStream(), guard).also { it.deadlineNanos = deadline },
            )
            val output = BufferedOutputStream(GuardedOutputStream(socket.getOutputStream(), guard))
            val headers = mutableListOf(
                "Host" to endpoint.hostHeader,
                "Accept" to "application/json",
                "User-Agent" to RawWebSocket.USER_AGENT,
                "Connection" to "close",
            )
            val key = apiKey
            if (withKey && key != null) headers += "Authorization" to "Bearer ${key.secret}"
            MiniHttp.writeRequest(output, "GET", path, headers)
            val head = MiniHttp.readResponseHead(input)
            // A redirect's body is never needed; nothing is read from it or followed.
            val body = if (head.status == 200) MiniHttp.readBody(input, head, maxBodyBytes) else ByteArray(0)
            return Response(head.status, head.header("content-type"), body)
        }
    }

    override fun toString(): String = "DashboardRestClient($endpoint)"

    companion object {
        const val HEALTH = "/api/health"
        const val STATUS = "/api/status"
    }
}
