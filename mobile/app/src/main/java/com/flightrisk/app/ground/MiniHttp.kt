package com.flightrisk.app.ground

import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/** A response or message was larger than the limit, so it was not read. */
class MessageTooLargeException(val limitBytes: Long) : IOException("message larger than $limitBytes bytes")

/** The peer broke the HTTP or WebSocket protocol. */
class ProtocolViolationException(message: String) : IOException(message)

/** The status line and headers of an HTTP response. Header names are lower case. */
class HttpResponseHead(val status: Int, val headers: Map<String, List<String>>) {
    fun header(name: String): String? = headers[name.lowercase()]?.firstOrNull()
    override fun toString(): String = "HttpResponseHead(status=$status)"
}

/**
 * The few pieces of HTTP/1.1 the ground clients need: one request with no
 * body, and a response read with hard size limits. Redirects are never
 * followed. The request carries no `Origin` header (the dashboard refuses a
 * foreign origin) and no cookies.
 */
object MiniHttp {
    /** The most bytes of status line and headers read. */
    const val MAX_HEAD_BYTES = 16 * 1024

    /** The most header lines read. */
    const val MAX_HEADERS = 100

    /** Writes a request line and headers, then flushes. Header values must already be validated. */
    fun writeRequest(out: OutputStream, method: String, path: String, headers: List<Pair<String, String>>) {
        val sb = StringBuilder()
        sb.append(method).append(' ').append(path).append(" HTTP/1.1\r\n")
        for ((name, value) in headers) {
            require(name.none { it.code < 0x21 || it.code > 0x7e || it == ':' }) { "bad header name" }
            // The value is not echoed: it may be a credential.
            require(value.none { it == '\r' || it == '\n' || it.code == 0 }) { "bad value for header $name" }
            sb.append(name).append(": ").append(value).append("\r\n")
        }
        sb.append("\r\n")
        out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
        out.flush()
    }

    /**
     * Reads the status line and headers, byte by byte so nothing after the
     * blank line is consumed (a WebSocket's first frame may follow at once).
     */
    fun readResponseHead(input: InputStream): HttpResponseHead {
        val lines = mutableListOf<String>()
        val line = ByteArrayOutputStream()
        var total = 0
        var previous = -1
        while (true) {
            val b = input.read()
            if (b < 0) throw EOFException("the connection closed during the HTTP response head")
            total++
            if (total > MAX_HEAD_BYTES) throw ProtocolViolationException("the HTTP response head is larger than $MAX_HEAD_BYTES bytes")
            if (b == '\n'.code && previous == '\r'.code) {
                val bytes = line.toByteArray()
                val text = String(bytes, 0, bytes.size - 1, Charsets.ISO_8859_1)
                line.reset()
                if (text.isEmpty()) break
                lines += text
                if (lines.size > MAX_HEADERS + 1) throw ProtocolViolationException("too many HTTP response headers")
            } else {
                line.write(b)
            }
            previous = b
        }
        if (lines.isEmpty()) throw ProtocolViolationException("empty HTTP response")
        val statusLine = lines.first()
        val parts = statusLine.split(' ', limit = 3)
        if (parts.size < 2 || !parts[0].startsWith("HTTP/1.")) {
            throw ProtocolViolationException("not an HTTP/1.x status line")
        }
        val status = parts[1].toIntOrNull()?.takeIf { it in 100..599 }
            ?: throw ProtocolViolationException("bad HTTP status code")
        val headers = linkedMapOf<String, MutableList<String>>()
        for (h in lines.drop(1)) {
            val colon = h.indexOf(':')
            if (colon <= 0) throw ProtocolViolationException("malformed HTTP header line")
            headers.getOrPut(h.substring(0, colon).trim().lowercase()) { mutableListOf() } += h.substring(colon + 1).trim()
        }
        return HttpResponseHead(status, headers)
    }

    /**
     * Reads the body that follows [head]: by `Content-Length`, by chunked
     * transfer encoding, or up to the end of the stream. Throws
     * [MessageTooLargeException] as soon as the body would exceed [maxBytes],
     * before reading the excess.
     */
    fun readBody(input: InputStream, head: HttpResponseHead, maxBytes: Int): ByteArray {
        val te = head.header("transfer-encoding")
        val out = ByteArrayOutputStream()
        if (te != null) {
            if (!te.equals("chunked", ignoreCase = true)) throw ProtocolViolationException("unsupported transfer encoding")
            readChunked(input, out, maxBytes)
            return out.toByteArray()
        }
        val lengthHeader = head.header("content-length")
        if (lengthHeader != null) {
            val length = lengthHeader.toLongOrNull()?.takeIf { it >= 0 }
                ?: throw ProtocolViolationException("bad Content-Length")
            if (length > maxBytes) throw MessageTooLargeException(maxBytes.toLong())
            copyExactly(input, out, length.toInt())
            return out.toByteArray()
        }
        val buffer = ByteArray(8192)
        while (true) {
            val n = input.read(buffer, 0, minOf(buffer.size, maxBytes + 1 - out.size()))
            if (n < 0) break
            out.write(buffer, 0, n)
            if (out.size() > maxBytes) throw MessageTooLargeException(maxBytes.toLong())
        }
        return out.toByteArray()
    }

    private fun readChunked(input: InputStream, out: ByteArrayOutputStream, maxBytes: Int) {
        while (true) {
            val sizeLine = readLine(input, 1024)
            val size = sizeLine.substringBefore(';').trim().toLongOrNull(16)?.takeIf { it >= 0 }
                ?: throw ProtocolViolationException("bad chunk size")
            if (size == 0L) {
                // Trailers, up to the blank line.
                var trailers = 0
                while (readLine(input, 1024).isNotEmpty()) {
                    if (++trailers > MAX_HEADERS) throw ProtocolViolationException("too many trailers")
                }
                return
            }
            // A subtraction, so a huge chunk size cannot wrap around.
            if (size > maxBytes - out.size()) throw MessageTooLargeException(maxBytes.toLong())
            copyExactly(input, out, size.toInt())
            if (readLine(input, 2).isNotEmpty()) throw ProtocolViolationException("missing CRLF after a chunk")
        }
    }

    private fun readLine(input: InputStream, max: Int): String {
        val sb = StringBuilder()
        while (true) {
            val b = input.read()
            if (b < 0) throw EOFException("the connection closed inside a chunked body")
            if (b == '\n'.code) return sb.toString().removeSuffix("\r")
            sb.append(b.toChar())
            if (sb.length > max) throw ProtocolViolationException("line too long in a chunked body")
        }
    }

    internal fun copyExactly(input: InputStream, out: OutputStream, count: Int) {
        val buffer = ByteArray(minOf(8192, maxOf(count, 1)))
        var remaining = count
        while (remaining > 0) {
            val n = input.read(buffer, 0, minOf(buffer.size, remaining))
            if (n < 0) throw EOFException("the connection closed before the end of the body")
            out.write(buffer, 0, n)
            remaining -= n
        }
    }
}
