package com.flightrisk.app.ground

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean

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
     * Opens a connection to [endpoint]. Cancelling the calling coroutine
     * aborts the attempt at once. [secrets] are redacted from any text the
     * connection reports (a server's close reason).
     *
     * @throws AddressNotAllowedException when the host is not on a private network.
     * @throws IOException when the connection or the WebSocket upgrade fails.
     */
    suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection
}

/**
 * One open connection: text frames in and out. [receive], [send] and
 * [closeGracefully] do their blocking IO on an IO dispatcher, so they may be
 * called from any thread; [close] does no IO at all.
 */
interface SocketIoConnection {
    /**
     * The next text frame. Throws [MessageTooLargeException] for an
     * oversized message, [WebSocketClosedException] when the server closes,
     * and [IOException] when the link drops or [close] was called.
     */
    suspend fun receive(): String

    /** Sends one text frame, or throws [IOException]. Never queues for later. */
    suspend fun send(frame: String)

    /** Sends a WebSocket close frame (best effort) and closes the connection. */
    suspend fun closeGracefully()

    /**
     * Closes the connection at once without writing anything; a pending
     * [receive] then fails. Safe on any thread, including Android's main
     * thread, and never throws.
     */
    fun close()
}

/**
 * Runs the blocking [block] on [socket] on [io], and closes the socket the
 * moment the calling coroutine is cancelled, so a blocked connect or read
 * cannot outlive its caller.
 */
internal suspend fun <T> cancellableSocketIo(socket: Socket, io: CoroutineDispatcher, block: () -> T): T =
    coroutineScope {
        val finished = AtomicBoolean(false)
        val closer = launch {
            try {
                awaitCancellation()
            } finally {
                if (!finished.get()) {
                    try {
                        socket.close()
                    } catch (_: Exception) {
                    }
                }
            }
        }
        try {
            withContext(io) { block() }
        } finally {
            finished.set(true)
            closer.cancel()
        }
    }

/**
 * The production [SocketIoPort]: [RawWebSocket] to an address checked by
 * [LanAddressPolicy].
 *
 * @param maxMessageBytes the largest WebSocket message accepted; a larger one
 *   fails the connection with [MessageTooLargeException] before its payload is
 *   read (default 8 MiB, below the dashboard's own Engine.IO `maxPayload` of
 *   10 MiB).
 * @param connectTimeoutMs one overall deadline for the TCP connect and the
 *   whole upgrade response.
 * @param guard called before every blocking socket operation (tests only).
 */
class WebSocketSocketIoPort(
    private val maxMessageBytes: Long = DEFAULT_MAX_MESSAGE_BYTES,
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 150_000,
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
                path = GroundEndpoint.SOCKET_IO_PATH,
                maxMessageBytes = maxMessageBytes,
                timeoutMs = connectTimeoutMs,
                readTimeoutMs = readTimeoutMs,
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
        override suspend fun send(frame: String) = withContext(io) { ws.sendText(frame) }
        override suspend fun closeGracefully() = withContext(io) { ws.close() }
        override fun close() = ws.abort()
        override fun toString(): String = "SocketIoConnection($ws)"
    }

    companion object {
        /** 8 MiB. */
        const val DEFAULT_MAX_MESSAGE_BYTES: Long = 8L * 1024 * 1024
    }
}
