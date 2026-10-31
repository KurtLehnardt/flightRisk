package com.flightrisk.app.transport

import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.HostResolver
import com.flightrisk.app.ground.IoGuard
import com.flightrisk.app.ground.LanAddressPolicy
import com.flightrisk.app.ground.RawWebSocket
import com.flightrisk.app.ground.SocketIoConnection
import com.flightrisk.app.ground.SocketIoPort
import com.flightrisk.app.ground.cancellableSocketIo
import com.flightrisk.contract.DetectionsWire
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.Socket

/**
 * The production connection to a ground station's edge port: a
 * [RawWebSocket] to an address that [LanAddressPolicy] has checked, never an
 * OkHttp socket (the network security config blocks cleartext to a LAN
 * address, and OkHttp has no inbound size limit).
 *
 * One limit applies both ways: a message may not be longer than the edge
 * link's own 1 MiB cap ([DetectionsWire.MAX_BYTES]), so a hostile ground
 * cannot make the phone buffer more than that, checked from the frame header
 * before the payload is read. There is no read timeout, because a v1 ground
 * may stay silent for a long time; a dead link shows as a failed or timed-out
 * send instead.
 *
 * Sends are cancellable: cancelling the caller closes the socket, so a
 * stalled write cannot outlive its timeout.
 */
class EdgeSocketPort(
    private val connectTimeoutMs: Int = 10_000,
    private val resolver: HostResolver = HostResolver.SYSTEM,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val guard: IoGuard = IoGuard.NONE,
) : SocketIoPort {

    override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
        val socket = Socket()
        val ws = cancellableSocketIo(socket, ioDispatcher) {
            guard.beforeBlockingIo("resolve")
            val address = LanAddressPolicy.resolve(endpoint.host, resolver)
            RawWebSocket.connect(
                address = address,
                port = endpoint.port,
                hostHeader = endpoint.hostHeader,
                path = PATH,
                maxMessageBytes = DetectionsWire.MAX_BYTES.toLong(),
                timeoutMs = connectTimeoutMs,
                readTimeoutMs = 0,
                secrets = secrets,
                guard = guard,
                socket = socket,
            )
        }
        return Connection(ws, socket, ioDispatcher)
    }

    private class Connection(
        private val ws: RawWebSocket,
        private val socket: Socket,
        private val io: CoroutineDispatcher,
    ) : SocketIoConnection {
        override suspend fun receive(): String = cancellableSocketIo(socket, io) { ws.receiveText() }
        override suspend fun send(frame: String) = cancellableSocketIo(socket, io) { ws.sendText(frame) }
        override suspend fun closeGracefully() = withContext(io) { ws.close() }
        override fun close() = ws.abort()
        override fun toString(): String = "EdgeConnection($ws)"
    }

    companion object {
        /** The ground's edge server accepts any path. */
        const val PATH = "/"
    }
}
