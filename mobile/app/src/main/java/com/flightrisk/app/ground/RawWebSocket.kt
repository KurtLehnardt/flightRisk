package com.flightrisk.app.ground

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** The server refused the WebSocket upgrade with an HTTP status other than 101. */
class HandshakeRejectedException(val status: Int) : IOException("the server refused the WebSocket upgrade with HTTP $status")

/**
 * The server closed the WebSocket with a close frame. [reason] is redacted
 * with the secrets the connection was given and holds at most 123 bytes of
 * text (the protocol's limit), so it is never truncated.
 */
class WebSocketClosedException(val code: Int?, val reason: String) :
    IOException("the server closed the WebSocket" + (code?.let { " with code $it" } ?: "") + (if (reason.isEmpty()) "" else ": $reason"))

/**
 * Called before every blocking socket operation with its name (`connect`,
 * `read`, `write`). Production code passes [NONE]; tests use it to prove that
 * no blocking IO runs on a thread that stands for Android's main thread.
 */
fun interface IoGuard {
    fun beforeBlockingIo(operation: String)

    companion object {
        val NONE: IoGuard = IoGuard { }
    }
}

/**
 * Socket reads with one overall deadline: before each read the socket timeout
 * is set to the time left, so a peer that trickles one byte at a time cannot
 * stretch a handshake or a REST answer past the deadline. Disarm it with
 * [deadlineNanos] = 0 after the handshake.
 */
internal class DeadlineInputStream(
    private val socket: Socket,
    private val raw: InputStream,
    private val guard: IoGuard,
) : InputStream() {
    @Volatile var deadlineNanos: Long = 0

    /** The read timeout used when no deadline is armed (0 = none). */
    @Volatile var idleTimeoutMs: Int = 0

    private fun arm() {
        guard.beforeBlockingIo("read")
        val deadline = deadlineNanos
        if (deadline == 0L) {
            socket.soTimeout = idleTimeoutMs
            return
        }
        val leftMs = (deadline - System.nanoTime()) / 1_000_000
        if (leftMs <= 0) throw SocketTimeoutException("the overall deadline passed")
        socket.soTimeout = leftMs.coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
    }

    override fun read(): Int {
        arm()
        return raw.read()
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
        arm()
        return raw.read(b, off, len)
    }

    override fun available(): Int = raw.available()
    override fun close() = raw.close()
}

internal class GuardedOutputStream(private val raw: OutputStream, private val guard: IoGuard) : OutputStream() {
    override fun write(b: Int) {
        guard.beforeBlockingIo("write")
        raw.write(b)
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
        guard.beforeBlockingIo("write")
        raw.write(b, off, len)
    }

    override fun flush() {
        guard.beforeBlockingIo("write")
        raw.flush()
    }

    override fun close() = raw.close()
}

/**
 * A minimal RFC 6455 WebSocket client for plain `ws://` on the local network.
 *
 * It exists instead of OkHttp's WebSocket for two reasons. First, OkHttp's
 * reader assembles every message in memory before handing it over and has no
 * inbound size limit, so a hostile peer could make the app buffer a message of
 * any size; this client reads each frame's declared length first and fails
 * with [MessageTooLargeException] before it reads the payload. Second, OkHttp
 * honours the network security config, which cannot express "private address
 * ranges only" and would therefore have to allow cleartext everywhere; this
 * client instead connects only to an address [LanAddressPolicy] has checked.
 *
 * The upgrade request carries no `Origin` header and offers no extensions or
 * subprotocols, so the server never compresses. Text messages only; binary
 * messages are read within the limit and discarded.
 *
 * [abort] closes the socket without writing anything and is safe on any
 * thread, including Android's main thread. [close] also writes a close frame
 * first and therefore blocks: call it from an IO thread.
 */
class RawWebSocket private constructor(
    private val socket: Socket,
    private val input: DataInputStream,
    private val output: BufferedOutputStream,
    private val maxMessageBytes: Long,
    private val secrets: List<String>,
) {
    private val random = SecureRandom()
    private val writeLock = ReentrantLock()

    @Volatile
    private var closeSent = false

    @Volatile
    private var closed = false

    /**
     * Reads the next complete text message. Answers pings, skips pongs and
     * binary messages, and throws [WebSocketClosedException] on a close
     * frame, [MessageTooLargeException] when a message would exceed the
     * limit, [ProtocolViolationException] on a protocol error, and
     * [IOException] when the connection drops or [abort] is called.
     * Call it from one IO thread at a time.
     */
    fun receiveText(): String {
        var message: ByteArrayOutputStream? = null
        var messageIsText = false
        while (true) {
            val b0 = input.readUnsignedByte()
            val b1 = input.readUnsignedByte()
            val fin = (b0 and 0x80) != 0
            if ((b0 and 0x70) != 0) throw ProtocolViolationException("reserved WebSocket bits set without an extension")
            val opcode = b0 and 0x0f
            if ((b1 and 0x80) != 0) throw ProtocolViolationException("the server masked a frame")
            val length: Long = when (val len7 = b1 and 0x7f) {
                126 -> input.readUnsignedShort().toLong()
                127 -> input.readLong().also { if (it < 0) throw ProtocolViolationException("bad 64-bit frame length") }
                else -> len7.toLong()
            }
            if (opcode >= 0x8) {
                if (!fin || length > 125) throw ProtocolViolationException("bad control frame")
                val payload = ByteArray(length.toInt())
                input.readFully(payload)
                when (opcode) {
                    OP_PING -> sendFrame(OP_PONG, payload)
                    OP_PONG -> Unit
                    OP_CLOSE -> handleClose(payload)
                    else -> throw ProtocolViolationException("unknown control opcode $opcode")
                }
                continue
            }
            when (opcode) {
                OP_TEXT, OP_BINARY -> {
                    if (message != null) throw ProtocolViolationException("a new message started inside a fragmented one")
                    message = ByteArrayOutputStream()
                    messageIsText = opcode == OP_TEXT
                }
                OP_CONTINUATION -> if (message == null) throw ProtocolViolationException("continuation frame without a message")
                else -> throw ProtocolViolationException("unknown data opcode $opcode")
            }
            val buffer = message!!
            // Checked against the declared length before a single payload byte is
            // read, written as a subtraction so a huge length cannot wrap around.
            if (length > maxMessageBytes - buffer.size()) throw MessageTooLargeException(maxMessageBytes)
            MiniHttp.copyExactly(input, buffer, length.toInt())
            if (!fin) continue
            message = null
            if (!messageIsText) continue
            return decodeUtf8(buffer.toByteArray())
        }
    }

    /** Sends one text message as a single masked frame. Blocking: call it from an IO thread. */
    fun sendText(text: String) {
        val bytes = text.toByteArray(Charsets.UTF_8)
        if (bytes.size > maxMessageBytes) throw MessageTooLargeException(maxMessageBytes)
        sendFrame(OP_TEXT, bytes)
    }

    /**
     * Sends a close frame (best effort, only when no other write is in
     * progress) and closes the socket. Blocking: call it from an IO thread.
     * Never throws.
     */
    fun close(code: Int = 1000) {
        if (closed) return
        try {
            if (!closeSent && !socket.isClosed && writeLock.tryLock()) {
                try {
                    sendFrame(OP_CLOSE, byteArrayOf((code shr 8).toByte(), code.toByte()))
                } finally {
                    writeLock.unlock()
                }
            }
        } catch (_: IOException) {
            // The socket is going away anyway.
        } catch (_: RuntimeException) {
        } finally {
            abort()
        }
    }

    /**
     * Closes the socket at once without writing; unblocks a pending
     * [receiveText]. Safe on any thread, including Android's main thread, and
     * never throws.
     */
    fun abort() {
        closed = true
        try {
            socket.close()
        } catch (_: IOException) {
        } catch (_: RuntimeException) {
        }
    }

    private fun handleClose(payload: ByteArray): Nothing {
        if (payload.size == 1) throw ProtocolViolationException("a close frame with a one-byte payload")
        val code = if (payload.size >= 2) ((payload[0].toInt() and 0xff) shl 8) or (payload[1].toInt() and 0xff) else null
        // At most 123 bytes: redacted with the secrets, never long enough to need truncation.
        val reason = if (payload.size > 2) {
            Redact.text(String(payload, 2, payload.size - 2, Charsets.UTF_8), secrets, maxLength = 1024)
        } else {
            ""
        }
        try {
            if (!closeSent) sendFrame(OP_CLOSE, payload.copyOfRange(0, minOf(2, payload.size)))
        } catch (_: IOException) {
        }
        abort()
        throw WebSocketClosedException(code, reason)
    }

    private fun sendFrame(opcode: Int, payload: ByteArray) {
        writeLock.withLock {
            if (closed) throw IOException("the WebSocket is closed")
            if (opcode == OP_CLOSE) closeSent = true
            val header = ByteArrayOutputStream(14)
            header.write(0x80 or opcode)
            val n = payload.size
            when {
                n <= 125 -> header.write(0x80 or n)
                n <= 0xffff -> {
                    header.write(0x80 or 126)
                    header.write(n shr 8)
                    header.write(n)
                }
                else -> {
                    header.write(0x80 or 127)
                    header.write(ByteBuffer.allocate(8).putLong(n.toLong()).array())
                }
            }
            val mask = ByteArray(4).also { random.nextBytes(it) }
            header.write(mask)
            val masked = ByteArray(n) { i -> (payload[i].toInt() xor mask[i and 3].toInt()).toByte() }
            output.write(header.toByteArray())
            output.write(masked)
            output.flush()
        }
    }

    private fun decodeUtf8(bytes: ByteArray): String =
        try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes))
                .toString()
        } catch (e: CharacterCodingException) {
            throw ProtocolViolationException("a text message is not valid UTF-8")
        }

    override fun toString(): String = "RawWebSocket(closed=$closed)"

    companion object {
        private const val OP_CONTINUATION = 0x0
        private const val OP_TEXT = 0x1
        private const val OP_BINARY = 0x2
        private const val OP_CLOSE = 0x8
        private const val OP_PING = 0x9
        private const val OP_PONG = 0xA
        private const val GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

        /**
         * Connects [socket] to [address]:[port] (an address the caller has
         * already checked) and performs the upgrade for [path], sending
         * [hostHeader] as the `Host`. No `Origin`, cookie or credential header
         * is sent. Blocking: call it from an IO thread. Closing [socket] from
         * another thread aborts it at once.
         *
         * @param timeoutMs one overall deadline for the TCP connect and the
         *   whole upgrade response, however slowly the server sends it.
         * @param readTimeoutMs the read timeout after the upgrade (0 = none);
         *   the caller's ping watchdog is the main dead-link detector.
         * @param secrets values to redact from a server's close reason.
         */
        fun connect(
            address: InetAddress,
            port: Int,
            hostHeader: String,
            path: String,
            maxMessageBytes: Long,
            timeoutMs: Int,
            readTimeoutMs: Int,
            secrets: List<String> = emptyList(),
            guard: IoGuard = IoGuard.NONE,
            socket: Socket = Socket(),
        ): RawWebSocket {
            require(maxMessageBytes in 1..Int.MAX_VALUE.toLong()) { "maxMessageBytes out of range" }
            try {
                val deadline = System.nanoTime() + timeoutMs * 1_000_000L
                socket.tcpNoDelay = true
                guard.beforeBlockingIo("connect")
                socket.connect(InetSocketAddress(address, port), timeoutMs)
                val timed = DeadlineInputStream(socket, socket.getInputStream(), guard).also { it.deadlineNanos = deadline }
                val input = DataInputStream(BufferedInputStream(timed, 16 * 1024))
                val output = BufferedOutputStream(GuardedOutputStream(socket.getOutputStream(), guard), 16 * 1024)
                val keyBytes = ByteArray(16).also { SecureRandom().nextBytes(it) }
                val key = Base64.getEncoder().encodeToString(keyBytes)
                MiniHttp.writeRequest(
                    output, "GET", path,
                    listOf(
                        "Host" to hostHeader,
                        "Upgrade" to "websocket",
                        "Connection" to "Upgrade",
                        "Sec-WebSocket-Key" to key,
                        "Sec-WebSocket-Version" to "13",
                        "User-Agent" to USER_AGENT,
                    ),
                )
                val head = MiniHttp.readResponseHead(input)
                if (head.status != 101) throw HandshakeRejectedException(head.status)
                if (!head.header("upgrade").equals("websocket", ignoreCase = true)) {
                    throw ProtocolViolationException("the upgrade response has no Upgrade: websocket header")
                }
                val connection = head.headers["connection"].orEmpty().joinToString(",")
                if (connection.split(',').none { it.trim().equals("upgrade", ignoreCase = true) }) {
                    throw ProtocolViolationException("the upgrade response has no Connection: upgrade header")
                }
                val expected = Base64.getEncoder().encodeToString(
                    MessageDigest.getInstance("SHA-1").digest((key + GUID).toByteArray(Charsets.ISO_8859_1)),
                )
                if (head.header("sec-websocket-accept") != expected) {
                    throw ProtocolViolationException("the upgrade response has a wrong Sec-WebSocket-Accept")
                }
                if (head.headers.containsKey("sec-websocket-extensions")) {
                    throw ProtocolViolationException("the server chose an extension the client did not offer")
                }
                if (head.headers.containsKey("sec-websocket-protocol")) {
                    throw ProtocolViolationException("the server chose a subprotocol the client did not offer")
                }
                timed.deadlineNanos = 0
                timed.idleTimeoutMs = readTimeoutMs
                return RawWebSocket(socket, input, output, maxMessageBytes, secrets)
            } catch (e: Throwable) {
                try {
                    socket.close()
                } catch (_: IOException) {
                }
                throw e
            }
        }

        /** The `User-Agent` of every ground request. */
        const val USER_AGENT = "FlightRisk-Android"
    }
}
