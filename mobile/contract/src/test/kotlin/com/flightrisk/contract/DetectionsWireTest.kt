package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

/** The phone-as-edge `detections` message against `edge_edge_to_ground.json`. */
class DetectionsWireTest {
    private fun fixture(case: String): JsonObject = Fixtures.payload("edge_edge_to_ground.json", case)

    private fun message(case: String): DetectionsMessage =
        DetectionsWire.decode(fixture(case)) ?: bad("$case did not decode")

    private fun parse(json: String): JsonObject = ContractJson.parseToJsonElement(json).jsonObject

    private fun ok(result: DetectionsWire.EncodeResult): DetectionsWire.EncodeResult.Ok =
        result as? DetectionsWire.EncodeResult.Ok ?: bad("expected Ok, got $result")

    @Test
    fun appearanceOnlyRoundTripsToTheFixture() {
        val encoded = ok(DetectionsWire.encode(message("detections_appearance_only")))
        assertTrue(jsonSemanticallyEqual(fixture("detections_appearance_only"), parse(encoded.json)))
        assertEquals(0, encoded.droppedCrops)
        assertFalse(encoded.droppedThumbnail)
    }

    @Test
    fun faceEmbeddingOnlyWhenFaceMatchingIsOn() {
        val m = message("detections_with_face")
        val on = parse(ok(DetectionsWire.encode(m, faceMatchingEnabled = true)).json)
        assertTrue(jsonSemanticallyEqual(fixture("detections_with_face"), on))
        // The same message with face matching off sends no face_embedding key at all.
        val off = parse(ok(DetectionsWire.encode(m, faceMatchingEnabled = false)).json)
        assertTrue(jsonSemanticallyEqual(fixture("detections_appearance_only"), off))
        assertFalse((off["detections"] as JsonArray)[0].jsonObject.containsKey("face_embedding"))
    }

    @Test
    fun noVideoSendsANullThumbnail() {
        val encoded = parse(ok(DetectionsWire.encode(message("detections_appearance_only"), streamVideo = false)).json)
        assertTrue(jsonSemanticallyEqual(fixture("detections_no_video"), encoded))
        assertEquals(JsonNull, encoded["thumbnail"])
    }

    @Test
    fun keysAreExactlyWhatThePythonEdgeWrites() {
        val withExtras = fixture("detections_appearance_only").toMutableMap().apply {
            put("drone_id", JsonPrimitive("drone-1"))
            put("detection_profile", JsonPrimitive("default"))
            put("face_model", JsonPrimitive("arcface"))
        }
        val m = DetectionsWire.decode(JsonObject(withExtras))!!
        for (face in listOf(false, true)) {
            val o = parse(ok(DetectionsWire.encode(m, faceMatchingEnabled = face)).json)
            assertEquals(DetectionsWire.MESSAGE_KEYS, o.keys)
            for (k in DetectionsWire.NEVER_SENT_KEYS) assertFalse(k, o.containsKey(k))
            val keys = (o["detections"] as JsonArray)[0].jsonObject.keys
            assertEquals(if (face) DetectionsWire.DETECTION_KEYS + "face_embedding" else DetectionsWire.DETECTION_KEYS, keys)
        }
    }

    @Test
    fun capMatchesTheFixtureAndIsAppliedToUtf8Bytes() {
        assertEquals(DetectionsWire.MAX_BYTES, (Fixtures.meta("edge_edge_to_ground.json")["max_message_bytes"] as JsonPrimitive).int)
        assertEquals(1_048_576, DetectionsWire.MAX_BYTES)
        val encoded = ok(DetectionsWire.encode(message("detections_appearance_only")))
        assertEquals(encoded.json.toByteArray(Charsets.UTF_8).size, encoded.bytes)
    }

    @Test
    fun overTheCapDropsCropsLargestFirstThenTheThumbnail() {
        val small = "A".repeat(100)
        val large = "B".repeat(4_000)
        val medium = "C".repeat(2_000)
        val m = DetectionsMessage(
            timestamp = 1761735600.25,
            frameId = 1,
            frameWidth = 1280,
            frameHeight = 720,
            thumbnail = "T".repeat(3_000),
            detections = listOf(
                det(small), det(large), det(medium),
            ),
        )
        val full = ok(DetectionsWire.encode(m, maxBytes = Int.MAX_VALUE))

        // Exactly the size without the large crop ("<crop>" becomes null): only the large crop goes.
        val cap1 = full.bytes - (large.length + 2 - 4)
        val r1 = ok(DetectionsWire.encode(m, maxBytes = cap1))
        assertEquals(1, r1.droppedCrops)
        assertFalse(r1.droppedThumbnail)
        val crops1 = crops(r1.json)
        assertEquals(listOf(small, null, medium), crops1)
        assertEquals(cap1, r1.bytes)
        // One byte less and the medium crop goes as well.
        assertEquals(2, ok(DetectionsWire.encode(m, maxBytes = cap1 - 1)).droppedCrops)

        // Without any crop it still does not fit: the thumbnail goes too.
        val cap2 = full.bytes - large.length - medium.length - small.length - 1_000
        val r2 = ok(DetectionsWire.encode(m, maxBytes = cap2))
        assertEquals(3, r2.droppedCrops)
        assertTrue(r2.droppedThumbnail)
        assertEquals(listOf(null, null, null), crops(r2.json))
        assertNull(parse(r2.json)["thumbnail"].stringValue())
        assertTrue(r2.bytes <= cap2)

        // Nothing left to drop: refuse.
        val r3 = DetectionsWire.encode(m, maxBytes = 200)
        assertTrue(r3 is DetectionsWire.EncodeResult.TooLarge)
    }

    @Test
    fun realCapRefusesAnOversizedEmbeddingMessage() {
        val huge = List(200_000) { 0.123456789 }
        val m = DetectionsMessage(1.0, 1, 640, 480, null, listOf(WireDetection(listOf(0.0, 0.0, 1.0, 1.0), 0.5, huge, null, null)))
        assertTrue(DetectionsWire.encode(m) is DetectionsWire.EncodeResult.TooLarge)
    }

    @Test
    fun nonFiniteValuesNeverThrow() {
        val good = det("A".repeat(10))
        val m = DetectionsMessage(
            timestamp = 1.0, frameId = 1, frameWidth = 640, frameHeight = 480, thumbnail = null,
            detections = listOf(
                good,
                good.copy(confidence = Double.NaN),
                good.copy(bbox = listOf(1.0, Double.POSITIVE_INFINITY, 3.0, 4.0)),
                good.copy(reidEmbedding = listOf(0.1, Double.NaN), faceEmbedding = listOf(Double.NEGATIVE_INFINITY)),
            ),
        )
        val r = ok(DetectionsWire.encode(m, faceMatchingEnabled = true))
        assertEquals(2, r.droppedDetections)
        assertEquals(2, r.droppedEmbeddings)
        val dets = parse(r.json)["detections"] as JsonArray
        assertEquals(2, dets.size)
        assertEquals(JsonNull, dets[1].jsonObject["reid_embedding"])
        assertEquals(JsonNull, dets[1].jsonObject["face_embedding"])
        assertFalse(r.json.contains("NaN"))
        assertFalse(r.json.contains("Infinity"))

        val badTime = DetectionsWire.encode(m.copy(timestamp = Double.NaN))
        assertTrue(badTime is DetectionsWire.EncodeResult.Invalid)
    }

    @Test
    fun slashesInImagesAreNotEscaped() {
        val m = DetectionsMessage(1.0, 1, 640, 480, "ab/cd+==", listOf(det("x/y")))
        val json = ok(DetectionsWire.encode(m)).json
        assertTrue(json.contains("\"thumbnail\":\"ab/cd+==\""))
        assertTrue(json.contains("\"crop\":\"x/y\""))
        assertFalse(json.contains("\\/"))
    }

    @Test
    fun base64MatchesPython() {
        val crop = (fixture("detections_appearance_only")["detections"] as JsonArray)[0].jsonObject["crop"].stringValue()!!
        val bytes = Base64.getDecoder().decode(crop)
        assertEquals(0xFF.toByte(), bytes[0])
        assertEquals(0xD8.toByte(), bytes[1])
        assertEquals(crop, DetectionsWire.base64Jpeg(bytes))
    }

    @Test
    fun decoderFollowsFromDict() {
        assertNull(DetectionsWire.decode("""{"type":"telemetry"}"""))
        assertNull(DetectionsWire.decode("""{"type":"detections","frame_id":1}"""))
        assertNull(DetectionsWire.decode("not json"))
        val m = DetectionsWire.decode("""{"type":"detections","timestamp":1.0,"frame_id":3,"thumbnail":"","detections":[]}""")!!
        assertEquals(0, m.frameWidth)
        assertNull(m.thumbnail)
        val noVideo = message("detections_no_video")
        assertNull(noVideo.thumbnail)
        assertEquals(17L, noVideo.frameId)
        assertEquals(listOf(512.0, 200.0, 640.0, 560.0), noVideo.detections[0].bbox)
    }

    private fun det(crop: String) = WireDetection(listOf(1.0, 2.0, 3.0, 4.0), 0.5, listOf(0.1, 0.2), null, crop)

    private fun crops(json: String): List<String?> =
        (parse(json)["detections"] as JsonArray).map { it.jsonObject["crop"].stringValue() }
}
