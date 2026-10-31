package com.flightrisk.app.ground

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException

/**
 * Opens text-frame connections to a dashboard's Socket.IO endpoint
 * (`ws://HOST:PORT/socket.io/?EIO=4&transport=websocket`). Each frame is one
 * Engine.IO packet as text; [GroundConsoleClient] speaks Engine.IO 4 and
 * Socket.IO 5 over it with the contract module's `SocketIoFrames` codec.
 *
 * This is the seam the JVM tests replace with a fake.
 */
interface SocketIoPort {
    /**
     * Opens a connection to [endpoint].
     *
     * @throws AddressNotAllowedException when the host is not on a private network.
     * @throws IOException when the connection or the WebSocket upgrade fails.
     */
    suspend fun open(endpoint: GroundEndpoint): SocketIoConnection
}

/** One open connection: text frames in and out. */
interface SocketIoConnection {
    /**
     * The next text frame. Throws [MessageTooLargeException] for an
     * oversized message, [WebSocketClosedException] when the server closes,
     * and [IOException] when the link drops or [close] was called.
     */
    suspend fun receive(): String

    /** Sends one text frame, or throws [IOException]. Never queues for later. */
    suspend fun send(frame: String)

    /** Closes the connection; a pending [receive] then fails. Safe to call repeatedly from any thread. */
    fun close()
}

/**
 * The production [SocketIoPort]: [RawWebSocket] to an address checked by
 * [LanAddressPolicy].
 *
 * @param maxMessageBytes the largest WebSocket message accepted; a larger one
 *   fails the connection with [MessageTooLargeException] before its payload is
 *   read (default 8 MiB, below the dashboard's own Engine.IO `maxPayload` of
 *   10 MiB).
 */
class WebSocketSocketIoPort(
    private val maxMessageBytes: Long = DEFAULT_MAX_MESSAGE_BYTES,
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 150_000,
    private val resolver: HostResolver = HostResolver.SYSTEM,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) : SocketIoPort {

    override suspend fun open(endpoint: GroundEndpoint): SocketIoConnection = withContext(ioDispatcher) {
        val address = LanAddressPolicy.resolve(endpoint.host, resolver)
        val ws = RawWebSocket.connect(
            address = address,
            port = endpoint.port,
            hostHeader = endpoint.hostHeader,
            path = GroundEndpoint.SOCKET_IO_PATH,
            maxMessageBytes = maxMessageBytes,
            timeoutMs = connectTimeoutMs,
            readTimeoutMs = readTimeoutMs,
        )
        Connection(ws, ioDispatcher)
    }

    private class Connection(private val ws: RawWebSocket, private val io: CoroutineDispatcher) : SocketIoConnection {
        override suspend fun receive(): String = withContext(io) { ws.receiveText() }
        override suspend fun send(frame: String) = withContext(io) { ws.sendText(frame) }
        override fun close() = ws.close()
        override fun toString(): String = "SocketIoConnection($ws)"
    }

    companion object {
        /** 8 MiB. */
        const val DEFAULT_MAX_MESSAGE_BYTES: Long = 8L * 1024 * 1024
    }
}
