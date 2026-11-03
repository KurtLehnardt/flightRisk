package com.flightrisk.app.transport

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.FakeConnection
import com.flightrisk.app.ground.eventually
import com.flightrisk.app.pipeline.PublishedDetection
import com.flightrisk.app.pipeline.PublishedFrame
import com.flightrisk.app.vision.EdgeEmbeddings
import com.flightrisk.app.vision.EmbeddingProvider
import com.flightrisk.app.vision.FakeCrop
import com.flightrisk.app.vision.FixtureEmbeddingProvider
import com.flightrisk.app.vision.NoEmbeddingProvider
import com.flightrisk.app.vision.fixtureReidVector
import com.flightrisk.contract.DetectionsWire
import com.flightrisk.contract.ReidEmbedding
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Appearance embeddings on the phone-as-edge link, against the Python ground's fixture. */
class EdgeEmbeddingPublishTest {

    private fun connected(h: EdgeHarness): FakeConnection = runBlocking {
        h.publisher.start()
        val c = h.port.connection(0)
        eventually("connected") { h.publisher.status.value == EdgeLinkStatus.Connected }
        c
    }

    private fun publishAndWait(h: EdgeHarness, c: FakeConnection, frame: PublishedFrame, expectedSent: Int) = runBlocking {
        h.publisher.publish(frame)
        eventually("sent $expectedSent") { c.sent.size >= expectedSent }
    }

    /** A frame built the way BitmapFrames builds it, with fake crops instead of bitmaps. */
    private fun frame(
        crops: List<FakeCrop>,
        provider: EmbeddingProvider<FakeCrop>,
        maxEmbeddings: Int,
        maxDetections: Int = Int.MAX_VALUE,
        id: Long = 18,
        timestamp: Double = 1761735600.75,
        boxes: (Int) -> List<Double> = { i -> listOf(10.0 * i, 20.0, 10.0 * i + 50.0, 220.0) },
        prepare: (FakeCrop) -> FakeCrop? = { it },
        release: (FakeCrop) -> Unit = {},
    ): PublishedFrame {
        val embeddings = EdgeEmbeddings.lazyEmbeddings(
            crops, { it.confidence }, maxEmbeddings, provider, prepare = prepare, release = release, maxDetections = maxDetections,
        )
        return PublishedFrame(
            frameId = id, timestampSeconds = timestamp, frameWidth = 1280, frameHeight = 720,
            detections = crops.mapIndexed { i, crop ->
                PublishedDetection(bbox = boxes(i), confidence = crop.confidence, reidEmbedding = embeddings[i])
            },
        )
    }

    private fun detections(text: String): List<JsonObject> =
        (parseObject(text)["detections"] as JsonArray).map { it.jsonObject }

    private fun vector(det: JsonObject): List<Double>? =
        (det["reid_embedding"] as? JsonArray)?.map { (it as JsonPrimitive).content.toDouble() }

    private fun sameJson(a: JsonElement?, b: JsonElement?): Boolean {
        if (a == null || a is JsonNull) return b == null || b is JsonNull
        if (b == null || b is JsonNull) return false
        return when (a) {
            is JsonObject -> b is JsonObject && a.keys == b.keys && a.keys.all { sameJson(a[it], b[it]) }
            is JsonArray -> b is JsonArray && a.size == b.size && a.indices.all { sameJson(a[it], b[it]) }
            is JsonPrimitive -> {
                val pb = b as? JsonPrimitive ?: return false
                when {
                    a.isString || pb.isString -> a.isString && pb.isString && a.content == pb.content
                    a.booleanOrNull != null || pb.booleanOrNull != null -> a.booleanOrNull == pb.booleanOrNull
                    else -> a.doubleOrNull != null && a.doubleOrNull == pb.doubleOrNull
                }
            }
        }
    }

    @Test
    fun `the phone sends exactly what the Python ground wrote into the fixture`() {
        // Same inputs as the exporter: two people, only the most confident gets an embedding.
        val h = EdgeHarness(config = EdgePublisherConfig(maxEmbeddingsPerMessage = 1))
        try {
            val c = connected(h)
            val provider = FixtureEmbeddingProvider(output = { _ -> fixtureReidVector().map { it.toFloat() }.toFloatArray() })
            val boxes = listOf(listOf(512.0, 200.0, 640.0, 560.0), listOf(100.0, 120.0, 150.0, 230.0))
            val f = frame(listOf(FakeCrop(0, 0.74), FakeCrop(1, 0.41)), provider, maxEmbeddings = 1, boxes = { boxes[it] })
            publishAndWait(h, c, f, 2)

            val sent = parseObject(c.sent[1])
            val fixture = ContractFixtures.case("edge_edge_to_ground.json", "detections_reid_embedding")["payload"] as JsonObject
            assertTrue("sent:\n${c.sent[1].take(400)}", sameJson(fixture, sent))
            val decoded = DetectionsWire.decode(c.sent[1])!!
            assertNull(ReidEmbedding.problem(decoded.detections[0].reidEmbedding))
            assertEquals(fixtureReidVector(), decoded.detections[0].reidEmbedding)
            assertNull(decoded.detections[1].reidEmbedding)
            runBlocking { eventually("counted after the write") { h.publisher.stats().embeddingsSent == 1L } }
            assertEquals(0, h.publisher.stats().embeddingsRejected)
        } finally {
            h.close()
        }
    }

    @Test
    fun `no provider sends null embeddings and runs nothing`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val crops = List(5) { FakeCrop(it, 0.5 + it / 10.0) }
            publishAndWait(h, c, frame(crops, NoEmbeddingProvider, maxEmbeddings = h.publisher.maxEmbeddings()), 2)
            val dets = detections(c.sent[1])
            assertEquals(5, dets.size)
            assertTrue(dets.all { it["reid_embedding"] == JsonNull })
            assertTrue(dets.all { it.keys == DetectionsWire.DETECTION_KEYS })
            assertEquals(0, h.publisher.stats().embeddingsSent)
        } finally {
            h.close()
        }
    }

    @Test
    fun `at most eight embeddings go out, for the most confident people, computed on the worker`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            assertEquals(8, h.publisher.maxEmbeddings())
            val provider = FixtureEmbeddingProvider()
            val crops = List(20) { FakeCrop(it, it / 20.0) }
            val f = frame(crops, provider, maxEmbeddings = h.publisher.maxEmbeddings())
            h.publisher.publish(f)
            assertTrue("the model does not run on the publishing thread", provider.threads.none { it === Thread.currentThread() })
            runBlocking { eventually("sent") { c.sent.size >= 2 } }

            val dets = detections(c.sent[1])
            assertEquals(20, dets.size)
            assertEquals((12..19).toList(), dets.indices.filter { dets[it]["reid_embedding"] != JsonNull })
            for (i in 12..19) assertNull(ReidEmbedding.problem(vector(dets[i])))
            assertEquals(8, provider.calls.size)
            assertTrue(provider.threads.none { it === Thread.currentThread() })
            runBlocking { eventually("counted after the write") { h.publisher.stats().embeddingsSent == 8L } }
            assertTrue(c.sent[1].toByteArray().size <= DetectionsWire.MAX_BYTES)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a failing or invalid model sends null, is counted and keeps the link up`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val throwing = FixtureEmbeddingProvider(output = { throw IllegalStateException("model failed") })
            publishAndWait(h, c, frame(listOf(FakeCrop(0, 0.9)), throwing, maxEmbeddings = 8, id = 1), 2)
            assertEquals(JsonNull, detections(c.sent[1])[0]["reid_embedding"])
            assertEquals(1, h.publisher.stats().embeddingsRejected)
            assertEquals(EdgeLinkStatus.Connected, h.publisher.status.value)

            h.clock.advance(1000)
            runBlocking { eventually("idle") { h.publisher.wantsFrame() } }
            val wrongLength = FixtureEmbeddingProvider(output = { FloatArray(768) { 0.03f } })
            publishAndWait(h, c, frame(listOf(FakeCrop(0, 0.9)), wrongLength, maxEmbeddings = 8, id = 2), 3)
            assertEquals(JsonNull, detections(c.sent[2])[0]["reid_embedding"])
            assertEquals(2, h.publisher.stats().embeddingsRejected)
            runBlocking { eventually("both frames counted") { h.publisher.stats().framesSent == 2L } }
            assertEquals(0, h.publisher.stats().embeddingsSent)
            assertFalse(c.closed)
        } finally {
            h.close()
        }
    }

    @Test
    fun `with more than 50 people both sides pick the same eight, so no prepared input is left over`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            assertEquals(50, h.publisher.maxDetections())
            // The most confident people are beyond the first 50, which the publisher leaves out.
            val crops = List(60) { i -> FakeCrop(i, if (i >= 50) 0.99 else i / 100.0) }
            val prepared = java.util.concurrent.CopyOnWriteArrayList<Int>()
            val released = java.util.concurrent.CopyOnWriteArrayList<Int>()
            val provider = FixtureEmbeddingProvider()
            val f = frame(
                crops, provider, maxEmbeddings = h.publisher.maxEmbeddings(), maxDetections = h.publisher.maxDetections(),
                prepare = { prepared += it.id; it }, release = { released += it.id },
            )
            assertEquals("inputs only for the top 8 of the first 50", (42..49).toSet(), prepared.toSet())
            publishAndWait(h, c, f, 2)

            val dets = detections(c.sent[1])
            assertEquals(50, dets.size)
            assertEquals((42..49).toList(), dets.indices.filter { dets[it]["reid_embedding"] != JsonNull })
            assertEquals("every prepared input was used and released", prepared.toSet(), released.toSet())
            assertEquals(prepared.size, released.size)
            assertEquals(8, provider.calls.size)
            assertEquals(10, h.publisher.stats().detectionsDropped)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a native link failure or an out-of-memory model sends null and the worker keeps going`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val failures = listOf<Throwable>(UnsatisfiedLinkError("no libmodel.so"), OutOfMemoryError("model buffers"), NoClassDefFoundError("x"))
            failures.forEachIndexed { n, failure ->
                if (n > 0) {
                    h.clock.advance(1000)
                    runBlocking { eventually("idle") { h.publisher.wantsFrame() } }
                }
                val f = PublishedFrame(
                    frameId = n.toLong(), timestampSeconds = 1.0, frameWidth = 10, frameHeight = 10,
                    detections = listOf(PublishedDetection(listOf(0.0, 0.0, 1.0, 1.0), 0.9, reidEmbedding = { throw failure })),
                )
                publishAndWait(h, c, f, n + 2)
                assertEquals(JsonNull, detections(c.sent[n + 1])[0]["reid_embedding"])
            }
            assertEquals(3, h.publisher.stats().embeddingsRejected)
            assertEquals(EdgeLinkStatus.Connected, h.publisher.status.value)
            assertFalse(c.closed)
        } finally {
            h.close()
        }
    }

    @Test
    fun `the publisher checks every vector itself before it is sent`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val unit = fixtureReidVector()
            val bad = listOf(
                listOf(0.25, -0.5),                                // wrong length
                unit.map { it * 3 },                               // not unit length
                unit.toMutableList().apply { this[0] = Double.NaN }, // not finite
                List(4097) { 0.01 },                               // far too long
            )
            val f = PublishedFrame(
                frameId = 1, timestampSeconds = 1.0, frameWidth = 10, frameHeight = 10,
                detections = (bad + listOf(unit)).mapIndexed { i, v ->
                    PublishedDetection(listOf(0.0, 0.0, 1.0, 1.0 + i), 0.9 - i / 100.0, reidEmbedding = { v })
                },
            )
            publishAndWait(h, c, f, 2)
            val dets = detections(c.sent[1])
            assertEquals(listOf(true, true, true, true, false), dets.map { it["reid_embedding"] == JsonNull })
            assertEquals(unit, vector(dets[4]))
            assertEquals(4, h.publisher.stats().embeddingsRejected)
            runBlocking { eventually("one sent") { h.publisher.stats().embeddingsSent == 1L } }
        } finally {
            h.close()
        }
    }

    @Test
    fun `no embedding value reaches a log line, the status, the stats or toString`() {
        val h = EdgeHarness()
        try {
            val c = connected(h)
            val v = fixtureReidVector()
            publishAndWait(h, c, frame(listOf(FakeCrop(0, 0.9)), FixtureEmbeddingProvider(), maxEmbeddings = 8), 2)
            c.serverError(java.io.IOException("drop"))
            runBlocking { eventually("reconnecting") { h.publisher.status.value is EdgeLinkStatus.Reconnecting } }
            val visible = h.stateDump()
            for (value in v.filter { kotlin.math.abs(it) > 1e-3 }.take(16)) assertFalse(value.toString(), visible.contains(value.toString().take(8)))
            assertFalse(visible.contains("reid_embedding"))
        } finally {
            h.close()
        }
    }
}
