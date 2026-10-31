package com.flightrisk.app.ground

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** The server refused the WebSocket upgrade with an HTTP status other than 101. */
class HandshakeRejectedException(val status: Int) : IOException("the server refused the WebSocket upgrade with HTTP $status")

/** The server closed the WebSocket with a close frame. [reason] is already redacted and truncated. */
class WebSocketClosedException(val code: Int?, val reason: String) :
    IOException("the server closed the WebSocket" + (code?.let { " with code $it" } ?: "") + (if (reason.isEmpty()) "" else ": $reason"))

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
 * The upgrade request carries no `Origin` header and offers no extensions,
 * so the server never compresses. Text messages only; binary messages are read
 * within the limit and discarded.
 */
class RawWebSocket private constructor(
    private val socket: Socket,
    private val input: DataInputStream,
    private val output: BufferedOutputStream,
    private val maxMessageBytes: Long,
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
     * [IOException] when the connection drops or [close] is called.
     * Call it from one thread at a time.
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
            // The limit is checked against the declared length before a single payload byte is read.
            if (buffer.size() + length > maxMessageBytes) throw MessageTooLargeException(maxMessageBytes)
            MiniHttp.copyExactly(input, buffer, length.toInt())
            if (!fin) continue
            message = null
            if (!messageIsText) continue
            return decodeUtf8(buffer.toByteArray())
        }
    }

    /** Sends one text message as a single masked frame. */
    fun sendText(text: String) {
        val bytes = text.toByteArray(Charsets.UTF_8)
        if (bytes.size > maxMessageBytes) throw MessageTooLargeException(maxMessageBytes)
        sendFrame(OP_TEXT, bytes)
    }

    /** Sends a close frame (best effort) and closes the socket. Safe to call more than once, from any thread. */
    fun close(code: Int = 1000) {
        if (closed) return
        try {
            // A sender blocked on a full TCP buffer holds the write lock; do not wait for it.
            if (!closeSent && !socket.isClosed && writeLock.tryLock(CLOSE_LOCK_WAIT_MS, TimeUnit.MILLISECONDS)) {
                try {
                    sendFrame(OP_CLOSE, byteArrayOf((code shr 8).toByte(), code.toByte()))
                } finally {
                    writeLock.unlock()
                }
            }
        } catch (_: IOException) {
            // The socket is going away anyway.
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        } finally {
            closed = true
            try {
                socket.close()
            } catch (_: IOException) {
            }
        }
    }

    /** Closes the socket at once without a close frame; unblocks a pending [receiveText]. */
    fun abort() {
        closed = true
        try {
            socket.close()
        } catch (_: IOException) {
        }
    }

    private fun handleClose(payload: ByteArray): Nothing {
        val code = if (payload.size >= 2) ((payload[0].toInt() and 0xff) shl 8) or (payload[1].toInt() and 0xff) else null
        val reason = if (payload.size > 2) {
            Redact.text(String(payload, 2, payload.size - 2, Charsets.UTF_8))
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
        private const val CLOSE_LOCK_WAIT_MS = 200L
        private const val GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

        /**
         * Connects to [address]:[port] (an address the caller has already
         * checked) and performs the upgrade for [path], sending [hostHeader]
         * as the `Host`. No `Origin`, cookie or credential header is sent.
         *
         * @param timeoutMs the limit for the TCP connect and for the upgrade response.
         * @param readTimeoutMs the read timeout after the upgrade (0 = none); the caller's
         *   own ping watchdog is the main dead-link detector.
         */
        fun connect(
            address: InetAddress,
            port: Int,
            hostHeader: String,
            path: String,
            maxMessageBytes: Long,
            timeoutMs: Int,
            readTimeoutMs: Int,
        ): RawWebSocket {
            require(maxMessageBytes in 1..Int.MAX_VALUE.toLong()) { "maxMessageBytes out of range" }
            val socket = Socket()
            try {
                socket.tcpNoDelay = true
                socket.connect(InetSocketAddress(address, port), timeoutMs)
                socket.soTimeout = timeoutMs
                val input = DataInputStream(BufferedInputStream(socket.getInputStream(), 16 * 1024))
                val output = BufferedOutputStream(socket.getOutputStream(), 16 * 1024)
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
                socket.soTimeout = readTimeoutMs
                return RawWebSocket(socket, input, output, maxMessageBytes)
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
