package com.flightrisk.app.transport

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.WebSocketClosedException
import com.flightrisk.app.ground.eventually
import com.flightrisk.contract.DetectionsWire
import com.flightrisk.contract.EdgeLink
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger

class EdgePublisherTest {

    private fun connected(h: EdgeHarness) = runBlocking {
        h.publisher.start()
        val c = h.port.connection(0)
        eventually("connected") { h.publisher.status.value == EdgeLinkStatus.Connected }
        c
    }

    private fun publishAndWait(h: EdgeHarness, c: com.flightrisk.app.ground.FakeConnection, frame: com.flightrisk.app.pipeline.PublishedFrame, expectedSent: Int) = runBlocking {
        h.publisher.publish(frame)
        eventually("sent $expectedSent") { c.sent.size >= expectedSent }
    }

    @Test
    fun `auth is the first frame, then detections, and the token appears nowhere else`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            publishAndWait(h, c, testFrame(), 2)
            assertEquals(EdgeLink.authMessage(TEST_TOKEN), c.sent[0])
            assertEquals("auth", typeOf(c.sent[0]))
            assertEquals("detections", typeOf(c.sent[1]))
            assertFalse(c.sent[1].contains(TEST_TOKEN))
            assertFalse("token in state dump", h.stateDump().contains(TEST_TOKEN))
            assertFalse(h.endpoint.toString().contains(TEST_TOKEN))
        } finally {
            h.close()
        }
    }

    @Test
    fun `no saved token means no auth frame`() {
        val h = EdgeHarness(token = null)
        try {
            val c = connected(h)
            publishAndWait(h, c, testFrame(), 1)
            assertEquals(listOf("detections"), c.sent.map(::typeOf))
        } finally {
            h.close()
        }
    }

    @Test
    fun `the detections message has exactly the contract keys and no drone_id or face data by default`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            publishAndWait(h, c, testFrame(face = listOf(0.1, 0.2)), 2)
            val msg = parseObject(c.sent[1])
            assertEquals(DetectionsWire.MESSAGE_KEYS, msg.keys)
            assertTrue(msg.keys.none { it in DetectionsWire.NEVER_SENT_KEYS })
            val det = (msg["detections"] as JsonArray)[0].jsonObject
            assertEquals(DetectionsWire.DETECTION_KEYS, det.keys)
            assertFalse(det.containsKey("face_embedding"))
            assertEquals(JsonNull, msg["thumbnail"])
            assertEquals("no pictures of people by default", JsonNull, det["crop"])
            assertEquals(1_700_000_000.5, (msg["timestamp"] as JsonPrimitive).content.toDouble(), 0.0)
        } finally {
            h.close()
        }
    }

    @Test
    fun `face embeddings are sent only when face matching is on`() {
        val h = EdgeHarness(face = true)
        try {
            val c = connected(h)
            publishAndWait(h, c, testFrame(face = listOf(0.1, 0.2)), 2)
            val det = (parseObject(c.sent[1])["detections"] as JsonArray)[0].jsonObject
            assertEquals(2, (det["face_embedding"] as JsonArray).size)
        } finally {
            h.close()
        }
    }

    @Test
    fun `only auth and detections are ever written`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            ContractFixtures.cases("edge_ground_to_edge.json").values.forEach { c.server(it["payload"].toString()) }
            c.server("not json")
            c.server("{\"type\":\"nack\"}")
            for (i in 1..3) {
                h.clock.advance(1000)
                h.publisher.publish(testFrame(id = i.toLong()))
                runBlocking { eventually("frame $i") { c.sent.size >= i + 1 } }
            }
            assertEquals(setOf("auth", "detections"), c.sent.map(::typeOf).toSet())
            assertTrue(c.sent.map(::typeOf).toSet().all { it in EdgeLink.OUTBOUND_TYPES })
            assertTrue(c.sent.map(::typeOf).none { it in EdgeLink.NEVER_SENT_TYPES })
        } finally {
            h.close()
        }
    }

    @Test
    fun `every ground message but stream_video is ignored and the link stays open`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            ContractFixtures.cases("edge_ground_to_edge.json")
                .filterKeys { it != "stream_video" }
                .values.forEach { c.server(it["payload"].toString()) }
            c.server("[1,2,3]")
            c.server("garbage")
            c.server("{\"type\":\"some_future_type\",\"x\":1}")
            c.server("{\"type\":\"stream_video\",\"enabled\":\"yes\"}")
            // A sentinel: once it is applied, every earlier message has been read.
            c.server("{\"type\":\"stream_video\",\"enabled\":true}")
            runBlocking { eventually("sentinel") { h.publisher.thumbnailsEnabled } }
            assertEquals(1, h.port.opened.size)
            assertFalse(c.closed)
            assertEquals(EdgeLinkStatus.Connected, h.publisher.status.value)
            assertEquals(listOf("auth"), c.sent.map(::typeOf))
        } finally {
            h.close()
        }
    }

    @Test
    fun `stream_video turns the thumbnail on and off and skips the image work while off`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val thumbCalls = AtomicInteger()
            publishAndWait(h, c, testFrame(id = 1, thumbCalls = thumbCalls), 2)
            assertEquals(JsonNull, parseObject(c.sent[1])["thumbnail"])
            assertEquals("no thumbnail work while off", 0, thumbCalls.get())

            c.server("{\"type\":\"stream_video\",\"enabled\":true}")
            runBlocking { eventually("on") { h.publisher.thumbnailsEnabled } }
            h.clock.advance(1000)
            publishAndWait(h, c, testFrame(id = 2, thumbCalls = thumbCalls), 3)
            assertTrue(parseObject(c.sent[2])["thumbnail"] is JsonPrimitive && parseObject(c.sent[2])["thumbnail"] !is JsonNull)
            assertEquals(1, thumbCalls.get())

            c.server("{\"type\":\"stream_video\",\"enabled\":false}")
            runBlocking { eventually("off") { !h.publisher.thumbnailsEnabled } }
            h.clock.advance(1000)
            publishAndWait(h, c, testFrame(id = 3, thumbCalls = thumbCalls), 4)
            assertEquals(JsonNull, parseObject(c.sent[3])["thumbnail"])
        } finally {
            h.close()
        }
    }

    @Test
    fun `an oversize frame is dropped and counted, and the next frame still goes out`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val huge = List(20_000) { 0.123456789 + it }
            h.publisher.publish(testFrame(id = 1, detections = 50, reid = huge))
            runBlocking { eventually("dropped") { h.publisher.stats().framesDroppedTooLarge == 1L } }
            assertEquals(0, h.publisher.stats().framesSent)
            assertEquals(listOf("auth"), c.sent.map(::typeOf))
            h.clock.advance(1000)
            runBlocking { eventually("not stuck busy after a drop") { h.publisher.wantsFrame() } }
            publishAndWait(h, c, testFrame(id = 2), 2)
            assertEquals(1, h.publisher.stats().framesSent)
            assertTrue(c.sent.all { it.toByteArray().size <= DetectionsWire.MAX_BYTES })
        } finally {
            h.close()
        }
    }

    @Test
    fun `crops, when switched on, are removed to fit and counted`() {
        val h = EdgeHarness(config = EdgePublisherConfig(sendCrops = true))
        try {
            val c = connected(h)
            val bigCrop = ByteArray(400_000) { (it % 251).toByte() }
            publishAndWait(h, c, testFrame(detections = 4, crop = bigCrop), 2)
            val s = h.publisher.stats()
            assertTrue("crops dropped: $s", s.cropsDropped > 0)
            assertTrue(c.sent[1].toByteArray().size <= DetectionsWire.MAX_BYTES)
        } finally {
            h.close()
        }
    }

    @Test
    fun `invalid detections and frames are dropped and counted`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            h.publisher.publish(testFrame(id = 1, timestamp = Double.NaN))
            runBlocking { eventually("invalid counted") { h.publisher.stats().framesDroppedInvalid == 1L } }
            h.clock.advance(1000)
            runBlocking { eventually("idle") { h.publisher.wantsFrame() } }
            val bad = com.flightrisk.app.pipeline.PublishedFrame(
                frameId = 2, timestampSeconds = 1.0, frameWidth = 1, frameHeight = 1, detections = listOf(
                    com.flightrisk.app.pipeline.PublishedDetection(listOf(Double.NaN, 0.0, 1.0, 1.0), 0.5),
                    com.flightrisk.app.pipeline.PublishedDetection(listOf(0.0, 0.0, 1.0, 1.0), 0.5),
                ),
            )
            publishAndWait(h, c, bad, 2)
            assertEquals(1, (parseObject(c.sent[1])["detections"] as JsonArray).size)
            assertEquals(1, h.publisher.stats().detectionsDropped)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a busy socket drops frames instead of queueing them`() {
        val gate = GateConnection().also { it.blockSends = true }
        val port = GatePort(gate)
        val h = EdgeHarness()
        val publisher = com.flightrisk.app.transport.EdgePublisher(
            endpoint = h.endpoint, tokenProvider = { tokenRead(TEST_TOKEN) }, scope = h.scope, port = port, clock = h.clock, random = { 0.0 },
        )
        try {
            publisher.start()
            runBlocking { eventually("connected") { publisher.status.value == EdgeLinkStatus.Connected } }
            // The auth frame is sent; the first detections frame then blocks in send.
            publisher.publish(testFrame(id = 1))
            runBlocking { eventually("auth sent") { gate.sent.size >= 1 } }
            runBlocking { eventually("frame 1 is in the blocked write") { !publisher.wantsFrame() } }
            for (i in 2..200) {
                h.clock.advance(1000)
                assertFalse("busy socket must not want frames", publisher.wantsFrame())
                publisher.publish(testFrame(id = i.toLong()))
            }
            assertEquals(199, publisher.stats().framesDroppedBusy)
            gate.release()
            runBlocking { eventually("released") { gate.sent.size >= 2 } }
            assertEquals("only one frame was ever pending", 2, gate.sent.size)
        } finally {
            publisher.stop()
            h.close()
        }
    }

    @Test
    fun `frames faster than the rate limit are dropped`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            publishAndWait(h, c, testFrame(id = 1), 2)
            runBlocking { eventually("first frame done") { h.publisher.stats().framesSent == 1L } }
            h.publisher.publish(testFrame(id = 2))
            assertEquals(1, h.publisher.stats().framesDroppedRate)
            assertFalse(h.publisher.wantsFrame())
            h.clock.advance(h.config.minFrameIntervalMs)
            assertTrue(h.publisher.wantsFrame())
            publishAndWait(h, c, testFrame(id = 3), 3)
        } finally {
            h.close()
        }
    }

    @Test
    fun `nothing is accepted while not connected`() {
        val h = EdgeHarness()
        try {
            assertFalse(h.publisher.wantsFrame())
            h.publisher.publish(testFrame())
            assertEquals(com.flightrisk.app.transport.EdgeStats(), h.publisher.stats())
            assertEquals(EdgeLinkStatus.Off, h.publisher.status.value)
        } finally {
            h.close()
        }
    }

    @Test
    fun `close code 4001 stops the link with the token message and never retries`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            c.serverError(WebSocketClosedException(EdgeLink.CLOSE_UNAUTHORIZED, "unauthorized"))
            runBlocking { eventually("rejected") { h.publisher.status.value == EdgeLinkStatus.TokenRejected } }
            repeat(5) { h.clock.advance(60_000) }
            Thread.sleep(100)
            assertEquals(1, h.port.opened.size)
            assertTrue(h.clock.sleepers().isEmpty())
            assertEquals(EdgeLinkStatus.TokenRejected, h.publisher.status.value)
            assertEquals("The ground rejected the edge token. Fix it in Settings.", GroundLinkText.TOKEN_REJECTED)
            assertTrue(c.closed)
            h.publisher.publish(testFrame())
            assertEquals(1, c.sent.size)
        } finally {
            h.close()
        }
    }

    @Test
    fun `other failures reconnect with bounded backoff that resets only after a stable link`() {
        val h = EdgeHarness()
        try {
            h.publisher.start()
            // Drop 1: fails quickly -> 1 s.
            var c = runBlocking { h.port.connection(0) }
            runBlocking { eventually("connected") { h.publisher.status.value == EdgeLinkStatus.Connected } }
            c.serverError(IOException("boom $TEST_TOKEN"))
            runBlocking { eventually("reconnecting 1") { (h.publisher.status.value as? EdgeLinkStatus.Reconnecting)?.attempt == 1 } }
            assertEquals(1_000L, (h.publisher.status.value as EdgeLinkStatus.Reconnecting).retryInMs)
            assertEquals(1, h.port.opened.size)
            runBlocking { eventually("sleeping") { h.clock.sleepers().isNotEmpty() } }
            h.clock.advance(999)
            Thread.sleep(50)
            assertEquals("no early retry", 1, h.port.opened.size)
            h.clock.advance(1)

            // Drop 2: still unstable -> 2 s.
            c = runBlocking { h.port.connection(1) }
            runBlocking { eventually("connected 2") { h.publisher.status.value == EdgeLinkStatus.Connected } }
            c.serverError(IOException("boom"))
            runBlocking { eventually("reconnecting 2") { (h.publisher.status.value as? EdgeLinkStatus.Reconnecting)?.attempt == 2 } }
            assertEquals(2_000L, (h.publisher.status.value as EdgeLinkStatus.Reconnecting).retryInMs)
            runBlocking { eventually("sleeping 2") { h.clock.sleepers().isNotEmpty() } }
            h.clock.advance(2_000)

            // Third link stays up for a while, so the next drop starts again at 1 s.
            c = runBlocking { h.port.connection(2) }
            runBlocking { eventually("connected 3") { h.publisher.status.value == EdgeLinkStatus.Connected } }
            h.clock.advance(h.config.minStableMs)
            c.serverError(IOException("boom"))
            runBlocking { eventually("reconnecting reset") { (h.publisher.status.value as? EdgeLinkStatus.Reconnecting)?.attempt == 1 } }
            assertEquals(1_000L, (h.publisher.status.value as EdgeLinkStatus.Reconnecting).retryInMs)
            assertFalse("token in state dump", h.stateDump().contains(TEST_TOKEN))
        } finally {
            h.close()
        }
    }

    @Test
    fun `backoff never exceeds its maximum`() {
        val h = EdgeHarness()
        try {
            h.port.failOpen = IOException("down")
            h.publisher.start()
            var last = 0L
            for (n in 1..12) {
                runBlocking { eventually("attempt $n") { (h.publisher.status.value as? EdgeLinkStatus.Reconnecting)?.attempt == n } }
                last = (h.publisher.status.value as EdgeLinkStatus.Reconnecting).retryInMs
                assertTrue(last <= h.config.backoff.maxMs)
                runBlocking { eventually("sleeping $n") { h.clock.sleepers().isNotEmpty() } }
                h.clock.advance(last)
            }
            assertEquals(h.config.backoff.maxMs, last)
        } finally {
            h.close()
        }
    }

    @Test
    fun `stop closes the connection and returns to off`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            h.publisher.stop()
            assertEquals(EdgeLinkStatus.Off, h.publisher.status.value)
            runBlocking { eventually("closed") { c.closed } }
            assertFalse(h.publisher.wantsFrame())
            h.publisher.publish(testFrame())
            assertEquals(1, c.sent.size)
        } finally {
            h.close()
        }
    }

    @Test
    fun `the port is given the token only so it can be redacted from server text`() {
        val port = RecordingPort()
        val h = EdgeHarness()
        val publisher = com.flightrisk.app.transport.EdgePublisher(
            endpoint = h.endpoint, tokenProvider = { tokenRead(TEST_TOKEN) }, scope = h.scope, port = port, clock = h.clock,
        )
        try {
            publisher.start()
            runBlocking { eventually("open") { port.connections.isNotEmpty() } }
            assertEquals(listOf(TEST_TOKEN), port.secretsSeen.single())
            assertNull(publisher.toString().takeIf { it.contains(TEST_TOKEN) })
        } finally {
            publisher.stop()
            h.close()
        }
    }

    @Test
    fun `no pictures of people are made or sent by default, even when asked for by the ground`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            c.server("{\"type\":\"stream_video\",\"enabled\":true}")
            runBlocking { eventually("video on") { h.publisher.thumbnailsEnabled } }
            val cropCalls = AtomicInteger()
            publishAndWait(h, c, testFrame(detections = 5, cropCalls = cropCalls), 2)
            val dets = parseObject(c.sent[1])["detections"] as JsonArray
            assertEquals(5, dets.size)
            assertTrue(dets.all { it.jsonObject["crop"] == JsonNull })
            assertEquals(0, cropCalls.get())
            assertEquals(0, h.publisher.maxCrops())
            assertTrue(h.publisher.wantsThumbnail())
        } finally {
            h.close()
        }
    }

    @Test
    fun `with crops on, at most 8 go out, for the most confident people, and all boxes stay`() {
        val h = EdgeHarness(config = EdgePublisherConfig(sendCrops = true))
        try {
            val c = connected(h)
            val frame = com.flightrisk.app.pipeline.PublishedFrame(
                frameId = 1, timestampSeconds = 1.0, frameWidth = 10, frameHeight = 10,
                detections = List(20) { i ->
                    com.flightrisk.app.pipeline.PublishedDetection(listOf(0.0, 0.0, 1.0, 1.0 + i), confidence = i / 20.0, cropJpeg = { byteArrayOf(1, 2) })
                },
            )
            assertEquals(8, h.publisher.maxCrops())
            publishAndWait(h, c, frame, 2)
            val dets = (parseObject(c.sent[1])["detections"] as JsonArray).map { it.jsonObject }
            assertEquals(20, dets.size)
            val withCrop = dets.indices.filter { dets[it]["crop"] != JsonNull }
            assertEquals((12..19).toList(), withCrop)
        } finally {
            h.close()
        }
    }

    @Test
    fun `images are compressed on the worker and not on the thread that publishes`() {
        val h = EdgeHarness(config = EdgePublisherConfig(sendCrops = true))
        try {
            val c = connected(h)
            c.server("{\"type\":\"stream_video\",\"enabled\":true}")
            runBlocking { eventually("video on") { h.publisher.thumbnailsEnabled } }
            val threads = java.util.concurrent.CopyOnWriteArrayList<Thread>()
            val frame = com.flightrisk.app.pipeline.PublishedFrame(
                frameId = 1, timestampSeconds = 1.0, frameWidth = 10, frameHeight = 10,
                thumbnailJpeg = { threads += Thread.currentThread(); byteArrayOf(1) },
                detections = listOf(com.flightrisk.app.pipeline.PublishedDetection(listOf(0.0, 0.0, 1.0, 1.0), 0.5, cropJpeg = { threads += Thread.currentThread(); byteArrayOf(2) })),
            )
            h.publisher.publish(frame)
            assertTrue("nothing compressed on the caller yet", threads.none { it === Thread.currentThread() })
            runBlocking { eventually("sent") { c.sent.size >= 2 } }
            assertEquals(2, threads.size)
            assertTrue(threads.none { it === Thread.currentThread() })
        } finally {
            h.close()
        }
    }

    @Test
    fun `without a token a non-loopback ground is not contacted and the status says why`() {
        val h = EdgeHarness(token = null, host = "192.168.1.20")
        try {
            h.publisher.start()
            runBlocking { eventually("needs token") { h.publisher.status.value == EdgeLinkStatus.NeedsToken } }
            repeat(3) { h.clock.advance(60_000) }
            Thread.sleep(100)
            assertEquals(0, h.port.opened.size)
            assertEquals("Save the edge token from the ground station first.", GroundLinkText.NEEDS_TOKEN)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a secure storage failure is reported on its own and nothing is contacted`() {
        val h = EdgeHarness(storageProblem = true, host = "192.168.1.20")
        try {
            h.publisher.start()
            runBlocking { eventually("storage") { h.publisher.status.value == EdgeLinkStatus.StorageUnavailable } }
            assertEquals(0, h.port.opened.size)
            assertEquals("Secure storage unavailable on this device", GroundLinkText.STORAGE_UNAVAILABLE)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a failed write does not hide a 4001 that is waiting to be read`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            // The write fails, and the ground's 4001 is the next thing the receive side sees.
            c.failSends = IOException("broken pipe")
            h.publisher.publish(testFrame())
            c.serverError(WebSocketClosedException(EdgeLink.CLOSE_UNAUTHORIZED, "unauthorized"))
            runBlocking { eventually("outcome") { h.publisher.status.value != EdgeLinkStatus.Connected } }
            // The receive side decides: either it saw the 4001 (no retry) or the dropped link (one retry), never a loop.
            Thread.sleep(100)
            repeat(3) { h.clock.advance(60_000); Thread.sleep(50) }
            assertTrue("at most one extra attempt: ${h.port.opened.size}", h.port.opened.size <= 2)
        } finally {
            h.close()
        }
    }

    @Test
    fun `once heartbeats are seen, silence ends the session`() {
        val h = EdgeHarness(config = EdgePublisherConfig(heartbeatDeadlineMs = 3_000))
        try {
            val c = connected(h)
            c.server("{\"type\":\"heartbeat\",\"seq\":1}")
            runBlocking { eventually("watchdog armed") { h.clock.sleepers().isNotEmpty() } }
            repeat(4) { h.clock.advance(1_000); Thread.sleep(30) }
            runBlocking { eventually("reconnecting") { h.publisher.status.value is EdgeLinkStatus.Reconnecting } }
            assertTrue((h.publisher.status.value as EdgeLinkStatus.Reconnecting).reason.contains("heartbeats"))
        } finally {
            h.close()
        }
    }

    @Test
    fun `without heartbeats there is no deadline`() {
        val h = EdgeHarness(config = EdgePublisherConfig(heartbeatDeadlineMs = 3_000))
        try {
            connected(h)
            repeat(10) { h.clock.advance(60_000) }
            Thread.sleep(100)
            assertEquals(EdgeLinkStatus.Connected, h.publisher.status.value)
        } finally {
            h.close()
        }
    }
}
