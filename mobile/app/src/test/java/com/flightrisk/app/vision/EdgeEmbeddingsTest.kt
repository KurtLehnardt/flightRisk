package com.flightrisk.app.vision

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.contract.ReidEmbedding
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

/** A person crop stand-in: the tests need no bitmaps. */
data class FakeCrop(val id: Int, val confidence: Double)

/**
 * The full-length appearance embedding the Python ground wrote into
 * `edge_edge_to_ground.json` (`detections_reid_embedding`).
 */
fun fixtureReidVector(): List<Double> {
    val payload = ContractFixtures.case("edge_edge_to_ground.json", "detections_reid_embedding")["payload"] as JsonObject
    return (payload["detections"] as JsonArray)[0].jsonObject["reid_embedding"]!!.jsonArray.map { it.jsonPrimitive.double }
}

/**
 * A deterministic provider: the fixture vector, scaled by a per-crop factor
 * (a raw model output is not unit length), and a record of every call.
 */
class FixtureEmbeddingProvider(
    override val modelId: String = ReidEmbedding.MODEL_ID,
    private val output: (FakeCrop) -> FloatArray? = { crop ->
        val v = fixtureReidVector()
        FloatArray(v.size) { (v[it] * (crop.id + 2)).toFloat() }
    },
) : EmbeddingProvider<FakeCrop> {
    val calls = CopyOnWriteArrayList<FakeCrop>()
    val threads = CopyOnWriteArrayList<Thread>()

    override fun embed(crop: FakeCrop): FloatArray? {
        calls += crop
        threads += Thread.currentThread()
        return output(crop)
    }
}

class EdgeEmbeddingsTest {
    private val crops = List(12) { FakeCrop(it, confidence = it / 12.0) }

    private fun lazy(
        provider: EmbeddingProvider<FakeCrop>,
        max: Int = 8,
        prepared: MutableList<FakeCrop> = mutableListOf(),
        released: MutableList<FakeCrop> = mutableListOf(),
    ) = EdgeEmbeddings.lazyEmbeddings(
        detections = crops,
        confidence = { it.confidence },
        max = max,
        provider = provider,
        prepare = { prepared += it; it.copy() },
        release = { released += it },
    )

    @Test
    fun `the ground's model gives the fixture vector, lazily, for the most confident detections only`() {
        val provider = FixtureEmbeddingProvider()
        val prepared = mutableListOf<FakeCrop>()
        val released = mutableListOf<FakeCrop>()
        val embeddings = lazy(provider, max = 3, prepared = prepared, released = released)

        assertEquals(crops.size, embeddings.size)
        assertEquals("inputs are prepared for the three most confident only", setOf(9, 10, 11), prepared.map { it.id }.toSet())
        assertTrue("nothing runs until the publisher asks", provider.calls.isEmpty())

        val values = embeddings.map { it() }
        for (i in 0 until 9) assertNull("detection $i", values[i])
        for (i in 9 until 12) {
            assertNull(ReidEmbedding.problem(values[i]))
            val expected = fixtureReidVector()
            for (k in expected.indices) assertEquals(expected[k], values[i]!![k], 1e-6)
        }
        assertEquals(setOf(9, 10, 11), provider.calls.map { it.id }.toSet())
        assertEquals("every prepared input is released after use", prepared.map { it.id }.sorted(), released.map { it.id }.sorted())

        // A second call does not run the model again.
        assertNull(embeddings[11]())
        assertEquals(3, provider.calls.size)
    }

    @Test
    fun `no provider means no work and no embeddings`() {
        val prepared = mutableListOf<FakeCrop>()
        val embeddings = EdgeEmbeddings.lazyEmbeddings(
            detections = crops, confidence = { it.confidence }, max = 8, provider = NoEmbeddingProvider,
            prepare = { prepared += it; it },
        )
        assertTrue(embeddings.all { it() == null })
        assertTrue(prepared.isEmpty())
        assertFalse(EdgeEmbeddings.usable(NoEmbeddingProvider))
        assertNull(NoEmbeddingProvider.embed(FakeCrop(0, 1.0)))
    }

    @Test
    fun `a provider for another model is never run`() {
        for (other in listOf("open_clip:ViT-B-32:openai", "osnet_x1_0", "", ReidEmbedding.MODEL_ID + " ")) {
            val provider = FixtureEmbeddingProvider(modelId = other)
            val prepared = mutableListOf<FakeCrop>()
            val embeddings = lazy(provider, prepared = prepared)
            assertTrue(other, embeddings.all { it() == null })
            assertTrue(other, provider.calls.isEmpty())
            assertTrue(other, prepared.isEmpty())
            assertFalse(other, EdgeEmbeddings.usable(provider))
        }
        assertTrue(EdgeEmbeddings.usable(FixtureEmbeddingProvider()))
    }

    @Test
    fun `a zero cap or no detections asks for nothing`() {
        val provider = FixtureEmbeddingProvider()
        assertTrue(lazy(provider, max = 0).all { it() == null })
        assertTrue(lazy(provider, max = -3).all { it() == null })
        assertTrue(EdgeEmbeddings.lazyEmbeddings(emptyList<FakeCrop>(), { it.confidence }, 8, provider, { it }).isEmpty())
        assertTrue(provider.calls.isEmpty())
    }

    @Test
    fun `a provider without a vector gives null, and an input that cannot be prepared is skipped`() {
        val provider = FixtureEmbeddingProvider(output = { null })
        assertTrue(lazy(provider, max = 2).all { it() == null })
        assertEquals(2, provider.calls.size)

        val running = FixtureEmbeddingProvider()
        val embeddings = EdgeEmbeddings.lazyEmbeddings(crops, { it.confidence }, 2, running, prepare = { null })
        assertTrue(embeddings.all { it() == null })
        assertTrue(running.calls.isEmpty())
    }

    @Test
    fun `an invalid vector is refused without naming a value, and the input is still released`() {
        val bad = listOf(
            FloatArray(256) { 0.4242f },
            FloatArray(ReidEmbedding.DIMENSION) { 0.4242f }.also { it[3] = Float.NaN },
            FloatArray(ReidEmbedding.DIMENSION) { 0.4242f }.also { it[3] = Float.POSITIVE_INFINITY },
            FloatArray(ReidEmbedding.DIMENSION),
        )
        for (vector in bad) {
            val released = mutableListOf<FakeCrop>()
            val embedding = lazy(FixtureEmbeddingProvider(output = { vector }), max = 1, released = released)[11]
            try {
                embedding()
                fail("accepted an invalid vector of ${vector.size} values")
            } catch (e: IllegalArgumentException) {
                assertFalse(e.message!!, e.message!!.contains("0.4242"))
            }
            assertEquals(1, released.size)
        }
    }

    @Test
    fun `a provider that throws releases its input and lets the error reach the publisher`() {
        val released = mutableListOf<FakeCrop>()
        val embedding = lazy(FixtureEmbeddingProvider(output = { throw IllegalStateException("model failed") }), max = 1, released = released)[11]
        try {
            embedding()
            fail("the failure was hidden")
        } catch (e: IllegalStateException) {
            assertEquals("model failed", e.message)
        }
        assertEquals(1, released.size)
    }
}
