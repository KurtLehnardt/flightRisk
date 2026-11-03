package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** The appearance embedding a phone edge sends, against `edge_edge_to_ground.json` (made by the Python ground). */
class ReidEmbeddingTest {
    private val file = "edge_edge_to_ground.json"
    private val caseName = "detections_reid_embedding"
    private fun spec(): JsonObject = Fixtures.meta(file)["reid_embedding"]?.jsonObject ?: bad("no _meta.reid_embedding")
    private fun payload(): JsonObject = Fixtures.payload(file, caseName)
    private fun fixtureVector(): List<Double> =
        payload()["detections"]!!.jsonArray[0].jsonObject["reid_embedding"]!!.jsonArray.map { it.jsonPrimitive.double }

    @Test
    fun theConstantsAreWhatTheGroundPublishes() {
        val s = spec()
        assertEquals(ReidEmbedding.LIBRARY, s["library"]!!.jsonPrimitive.content)
        assertEquals(ReidEmbedding.MODEL, s["model"]!!.jsonPrimitive.content)
        assertEquals(ReidEmbedding.PRETRAINED, s["pretrained"]!!.jsonPrimitive.content)
        assertEquals(ReidEmbedding.DIMENSION, s["dimension"]!!.jsonPrimitive.int)
        assertEquals(ReidEmbedding.INPUT_SIZE, s["input_size"]!!.jsonPrimitive.int)
        assertEquals("float32", s["dtype"]!!.jsonPrimitive.content)
        assertTrue(s["l2_normalized"]!!.jsonPrimitive.boolean)
        assertEquals(ReidEmbedding.MEAN, s["mean"]!!.jsonArray.map { it.jsonPrimitive.double })
        assertEquals(ReidEmbedding.STD, s["std"]!!.jsonArray.map { it.jsonPrimitive.double })
        assertEquals("open_clip:ViT-B-16:laion2b_s34b_b88k", ReidEmbedding.MODEL_ID)
    }

    @Test
    fun theFullLengthSampleDecodesValidatesAndRoundTrips() {
        val message = DetectionsWire.decode(payload()) ?: bad("$caseName did not decode")
        val vector = message.detections[0].reidEmbedding
        assertNull(ReidEmbedding.problem(vector))
        assertEquals(fixtureVector(), vector)
        assertNull("the second detection has no embedding", message.detections[1].reidEmbedding)

        val encoded = DetectionsWire.encode(message, streamVideo = false) as DetectionsWire.EncodeResult.Ok
        val json = ContractJson.parseToJsonElement(encoded.json).jsonObject
        assertTrue(jsonSemanticallyEqual(payload(), json))
        assertEquals(0, encoded.droppedEmbeddings)
        // Every value survives the trip through text exactly.
        val back = json["detections"]!!.jsonArray[0].jsonObject["reid_embedding"]!!.jsonArray.map { it.jsonPrimitive.double }
        assertEquals(fixtureVector(), back)
    }

    @Test
    fun toWireMakesExactlyTheFixtureVectorFromFloat32Values() {
        val floats = FloatArray(ReidEmbedding.DIMENSION) { fixtureVector()[it].toFloat() }
        assertEquals(fixtureVector(), ReidEmbedding.toWire(floats))
        // A raw, unnormalized output of the same direction gives the same wire vector (within float32 rounding).
        val scaled = FloatArray(floats.size) { floats[it] * 7.5f }
        val wire = ReidEmbedding.toWire(scaled)
        assertNull(ReidEmbedding.problem(wire))
        for (i in wire.indices) assertEquals(fixtureVector()[i], wire[i], 1e-6)
    }

    @Test
    fun theShortenedSamplesAreNotValidEmbeddings() {
        val shortened = DetectionsWire.decode(Fixtures.payload(file, "detections_appearance_only"))!!
        assertEquals("expected 512 values, got 4", ReidEmbedding.problem(shortened.detections[0].reidEmbedding))
    }

    @Test
    fun problemsAreFoundAndNameNoValue() {
        val good = fixtureVector()
        assertNull(ReidEmbedding.problem(good))
        assertNotNull(ReidEmbedding.problem(null))
        assertNotNull(ReidEmbedding.problem(good.dropLast(1)))
        assertNotNull(ReidEmbedding.problem(good + 0.0))
        assertNotNull(ReidEmbedding.problem(good.toMutableList().apply { this[3] = Double.NaN }))
        assertNotNull(ReidEmbedding.problem(good.toMutableList().apply { this[3] = Double.POSITIVE_INFINITY }))
        assertNotNull(ReidEmbedding.problem(good.map { it * 2 }))
        assertNotNull(ReidEmbedding.problem(List(ReidEmbedding.DIMENSION) { 0.0 }))

        val secretish = FloatArray(ReidEmbedding.DIMENSION) { 0.123456f }.also { it[7] = Float.NaN }
        for (raw in listOf(FloatArray(10), FloatArray(ReidEmbedding.DIMENSION), secretish)) {
            try {
                ReidEmbedding.toWire(raw)
                fail("accepted an invalid vector")
            } catch (e: IllegalArgumentException) {
                assertTrue(e.message!!, !e.message!!.contains("0.123"))
            }
        }
    }

    @Test
    fun toStringNamesSizesNotEmbeddingsOrPictures() {
        val message = DetectionsWire.decode(payload())!!.copy(thumbnail = "QUJDREVGR0g=")
        val withCrop = message.copy(detections = message.detections.map { it.copy(crop = "SklKS0xNTk8=", faceEmbedding = listOf(0.4242, 0.5353)) })
        val text = withCrop.toString()
        assertTrue(text, text.contains("reidEmbedding=<512 values>"))
        assertTrue(text, text.contains("faceEmbedding=<2 values>"))
        for (secret in fixtureVector().filter { kotlin.math.abs(it) > 1e-3 }.take(32).map { it.toString().take(8) } + listOf("0.4242", "QUJDREVG", "SklKS0xN")) {
            assertTrue("$secret leaked", !text.contains(secret))
        }
        assertTrue(text, text.contains("bbox=[512.0, 200.0, 640.0, 560.0]"))
    }

    @Test
    fun decodingBoundsAndChecksEmbeddings() {
        fun withEmbedding(value: kotlinx.serialization.json.JsonElement): DetectionsMessage? {
            val det = payload()["detections"]!!.jsonArray[0].jsonObject.toMutableMap().apply { put("reid_embedding", value) }
            val dets = JsonArray(listOf(JsonObject(det)))
            return DetectionsWire.decode(JsonObject(payload().toMutableMap().apply { put("detections", dets) }))
        }
        val tooLong = JsonArray(List(DetectionsWire.MAX_EMBEDDING_VALUES + 1) { JsonPrimitive(0.01) })
        assertNull(withEmbedding(tooLong)!!.detections[0].reidEmbedding)
        val atLimit = JsonArray(List(DetectionsWire.MAX_EMBEDDING_VALUES) { JsonPrimitive(0.01) })
        assertEquals(DetectionsWire.MAX_EMBEDDING_VALUES, withEmbedding(atLimit)!!.detections[0].reidEmbedding!!.size)
        // A non-number inside the array makes the whole embedding null, never a shorter vector.
        val mixed = JsonArray(fixtureVector().map(::JsonPrimitive).toMutableList<kotlinx.serialization.json.JsonElement>().apply { this[5] = JsonPrimitive("x") })
        assertNull(withEmbedding(mixed)!!.detections[0].reidEmbedding)
        assertNull(withEmbedding(JsonNull)!!.detections[0].reidEmbedding)
        assertNull(withEmbedding(JsonPrimitive("0.1,0.2"))!!.detections[0].reidEmbedding)
    }
}
