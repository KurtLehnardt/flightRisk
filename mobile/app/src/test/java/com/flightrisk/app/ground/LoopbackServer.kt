package com.flightrisk.app.ground

import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * A throwaway server on 127.0.0.1 with an OS-assigned port. Each accepted
 * connection runs [handler] on its own thread. Nothing leaves the machine.
 */
class LoopbackServer(private val handler: (Socket) -> Unit) : AutoCloseable {
    private val server = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
    val port: Int get() = server.localPort
    val accepted = AtomicInteger()
    val errors = CopyOnWriteArrayList<Throwable>()
    val handlersDone = AtomicInteger()

    init {
        thread(isDaemon = true, name = "loopback-accept") {
            while (!server.isClosed) {
                val socket = try {
                    server.accept()
                } catch (_: IOException) {
                    break
                }
                accepted.incrementAndGet()
                thread(isDaemon = true, name = "loopback-conn") {
                    try {
                        socket.use { handler(it) }
                    } catch (t: Throwable) {
                        errors += t
                    } finally {
                        handlersDone.incrementAndGet()
                    }
                }
            }
        }
    }

    override fun close() = server.close()
}

/** Reads request lines up to the blank line. */
fun readRequestHead(input: InputStream): List<String> {
    val lines = mutableListOf<String>()
    val line = ByteArrayOutputStream()
    var previous = -1
    while (true) {
        val b = input.read()
        if (b < 0) throw IOException("eof in request head")
        if (b == '\n'.code && previous == '\r'.code) {
            val bytes = line.toByteArray()
            val text = String(bytes, 0, bytes.size - 1, Charsets.ISO_8859_1)
            line.reset()
            if (text.isEmpty()) return lines
            lines += text
        } else {
            line.write(b)
        }
        previous = b
    }
}

fun headerValue(lines: List<String>, name: String): String? =
    lines.drop(1).firstOrNull { it.substringBefore(':').trim().equals(name, ignoreCase = true) }
        ?.substringAfter(':')?.trim()

/** Answers a WebSocket upgrade request with a correct 101. */
fun acceptUpgrade(lines: List<String>, out: OutputStream, acceptOverride: String? = null) {
    val key = headerValue(lines, "Sec-WebSocket-Key")!!
    val accept = acceptOverride ?: Base64.getEncoder().encodeToString(
        MessageDigest.getInstance("SHA-1").digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray()),
    )
    out.write(
        ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
            "Sec-WebSocket-Accept: $accept\r\n\r\n").toByteArray(),
    )
    out.flush()
}

/** Writes the header of an unmasked server frame. */
fun writeFrameHeader(out: OutputStream, opcode: Int, length: Long, fin: Boolean = true) {
    out.write((if (fin) 0x80 else 0) or opcode)
    when {
        length <= 125 -> out.write(length.toInt())
        length <= 0xffff -> {
            out.write(126)
            out.write((length shr 8).toInt() and 0xff)
            out.write(length.toInt() and 0xff)
        }
        else -> {
            out.write(127)
            for (shift in 56 downTo 0 step 8) out.write(((length shr shift) and 0xff).toInt())
        }
    }
}

/** Writes one unmasked server frame. */
fun writeServerFrame(out: OutputStream, opcode: Int, payload: ByteArray, fin: Boolean = true) {
    writeFrameHeader(out, opcode, payload.size.toLong(), fin)
    out.write(payload)
    out.flush()
}

fun writeServerText(out: OutputStream, text: String) = writeServerFrame(out, 0x1, text.toByteArray())

/** Reads one client frame, checks it is masked, and returns its opcode and unmasked payload. */
fun readClientFrame(input: InputStream): Pair<Int, ByteArray> {
    val din = DataInputStream(input)
    val b0 = din.readUnsignedByte()
    val b1 = din.readUnsignedByte()
    check((b1 and 0x80) != 0) { "client frame not masked" }
    val length = when (val l = b1 and 0x7f) {
        126 -> din.readUnsignedShort().toLong()
        127 -> din.readLong()
        else -> l.toLong()
    }
    val mask = ByteArray(4).also { din.readFully(it) }
    val payload = ByteArray(length.toInt()).also { din.readFully(it) }
    for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i and 3].toInt()).toByte()
    return (b0 and 0x0f) to payload
}

fun readClientText(input: InputStream): String {
    while (true) {
        val (op, payload) = readClientFrame(input)
        if (op == 0x1) return String(payload, Charsets.UTF_8)
    }
}

/**
 * Sends the header of a text message of [declaredBytes] and then streams
 * filler until the peer stops reading. Returns how many payload bytes were
 * written before the write failed.
 */
fun streamHugeMessage(out: OutputStream, declaredBytes: Long): Long {
    writeFrameHeader(out, 0x1, declaredBytes)
    out.flush()
    val chunk = ByteArray(64 * 1024) { 'A'.code.toByte() }
    var written = 0L
    try {
        while (written < declaredBytes) {
            val n = minOf(chunk.size.toLong(), declaredBytes - written).toInt()
            out.write(chunk, 0, n)
            written += n
        }
        out.flush()
    } catch (_: IOException) {
        // The client closed the connection, as it should.
    }
    return written
}
