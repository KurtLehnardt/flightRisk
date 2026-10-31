package com.flightrisk.app.transport

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
        val sends = Regex("""\.send\(([^)]*)\)|sendWithTimeout\([^,]+,\s*([^)]*)\)""").findAll(publisher).map { it.value }.toList()
        assertTrue(sends.isNotEmpty())
        assertTrue("Unexpected send: $sends", sends.all { it.contains("message") || it.contains("authMessage") || it.contains("frame") })
        assertTrue(publisher.contains("EdgeLink.authMessage(token)"))
        assertTrue(publisher.contains("DetectionsWire.encode"))
    }
}
