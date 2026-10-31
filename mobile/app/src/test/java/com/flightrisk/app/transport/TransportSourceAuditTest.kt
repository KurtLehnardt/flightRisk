package com.flightrisk.app.transport

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Source checks on the edge link code: one wire format, no OkHttp, no secret in a log call. */
class TransportSourceAuditTest {
    private val dir: File = listOf(File("src/main/java"), File("app/src/main/java"), File("mobile/app/src/main/java"))
        .map { File(it, "com/flightrisk/app/transport") }.first { it.isDirectory }

    private fun sources() = dir.listFiles { f -> f.extension == "kt" }!!.associate { it.name to it.readText() }

    @Test
    fun `the wire format exists only in the contract module`() {
        val keys = Regex("\"(frame_id|reid_embedding|face_embedding|frame_width|stream_video|drone_id)\"")
        val offenders = sources().filter { keys.containsMatchIn(it.value) }.keys
        assertTrue("Wire keys outside :contract: $offenders", offenders.isEmpty())
    }

    @Test
    fun `no OkHttp and no listener in the transport package`() {
        val offenders = sources().filter { Regex("okhttp3|WebSocketListener|ServerSocket").containsMatchIn(it.value) }.keys
        assertTrue("Offenders: $offenders", offenders.isEmpty())
    }

    @Test
    fun `no log call interpolates a token`() {
        val offenders = sources().filter { (_, text) ->
            Regex("""(log\(|Log\.[a-z]\()[^\n]*\$\{?(token|Token|secrets|activeSecrets)""").containsMatchIn(text)
        }.keys
        assertTrue("Log calls naming a token: $offenders", offenders.isEmpty())
    }

    @Test
    fun `the publisher writes only through the contract encoders`() {
        val publisher = sources().getValue("EdgePublisher.kt")
        val sends = Regex("""sendWithTimeout\(connection,\s*([^)]*\))?""").findAll(publisher)
            .map { it.value.replace(Regex("\\s+"), " ") }.filter { !it.contains("private") }.toList()
        // Exactly two call sites: the auth message and the encoded detections message.
        assertEquals(sends.toString(), setOf("sendWithTimeout(connection, EdgeLink.authMessage(token)", "sendWithTimeout(connection, message)"), sends.toSet())
        assertTrue(publisher.contains("DetectionsWire.encode"))
        assertTrue(Regex("""\.send\(""").findAll(publisher).count() == 1)
    }
}
