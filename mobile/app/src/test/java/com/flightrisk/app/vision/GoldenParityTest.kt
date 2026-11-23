package com.flightrisk.app.vision

import com.flightrisk.contract.ReidEmbedding
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * The app's detector and embedder code against the golden fixtures of
 * `make_models.py verify` (docs/mobile-models.md, "Golden fixtures for the
 * apps' unit tests"). The first group uses only the numbers committed in
 * `src/test/resources/golden/golden-numbers.json` and always runs; the second
 * needs the full fixtures (skipped without them); [ModelParityTest] runs the
 * models themselves.
 */
class GoldenParityTest {

    private fun assertSameDetections(expected: List<FrameBox>, actual: List<FrameBox>, boxTol: Float, scoreTol: Float) {
        assertEquals("detections: $actual", expected.size, actual.size)
        for ((e, a) in expected.zip(actual)) {
            assertEquals(e.classId, a.classId)
            assertEquals(e.score, a.score, scoreTol)
            assertEquals(e.x1, a.x1, boxTol)
            assertEquals(e.y1, a.y1, boxTol)
            assertEquals(e.x2, a.x2, boxTol)
            assertEquals(e.y2, a.y2, boxTol)
        }
    }

    private fun goldenLetterbox(): Letterbox {
        val (w, h) = GoldenFixtures.frameSize
        val lb = YoloxContract.scale(w, h)
        assertEquals(GoldenFixtures.letterboxRatio, lb.ratio, 0f)
        return lb
    }

    // ------------------------------------------------------------------
    // Committed numbers
    // ------------------------------------------------------------------

    @Test
    fun `decoding PyTorch's output gives the golden detections exactly`() {
        val all = YoloxContract.decodeAllClasses(GoldenFixtures.detectorOutputFromNumbers(), goldenLetterbox(), 0.25f)
        assertSameDetections(GoldenFixtures.goldenDetections(), all, boxTol = 0f, scoreTol = 0f)
        // The person decoder keeps the four people, best first.
        val persons = YoloxContract.decode(GoldenFixtures.detectorOutputFromNumbers(), longArrayOf(1, 8400, 85), goldenLetterbox(), 0.25f)
        assertEquals(GoldenFixtures.goldenDetections().filter { it.classId == COCO_PERSON }, persons)
    }

    @Test
    fun `the crop of the best person is cut where the ground cuts it`() {
        val (w, h) = GoldenFixtures.frameSize
        val best = YoloxContract.decode(GoldenFixtures.detectorOutputFromNumbers(), longArrayOf(1, 8400, 85), goldenLetterbox(), 0.25f).first()
        assertArrayEquals(GoldenFixtures.goldenCropPixels(), CropRule.pixels(best, w, h))
    }

    @Test
    fun `the golden embedding is the ground's model and passes the wire contract unchanged`() {
        assertEquals(ReidEmbedding.MODEL_ID, GoldenFixtures.numbers.getValue("embedder_model").jsonPrimitive.content)
        val e = GoldenFixtures.goldenEmbedding()
        assertEquals(ClipContract.EMBEDDING_DIM, e.size)
        val norm = kotlin.math.sqrt(e.sumOf { it.toDouble() * it })
        assertEquals(1.0, norm, 1e-5)
        val wire = ReidEmbedding.toWire(e)
        assertEquals(ReidEmbedding.DIMENSION, wire.size)
        assertEquals(1.0, ClipContract.cosine(e, wire.map { it.toFloat() }.toFloatArray()), 1e-9)
    }

    // ------------------------------------------------------------------
    // Full fixtures (build/mobile_models/golden)
    // ------------------------------------------------------------------

    @Test
    fun `the letterboxed tensor matches the golden detector input`() {
        val (frame, w, h) = GoldenFixtures.png("frame.png")
        val expected = GoldenFixtures.floats("detector_input.f32")
        val actual = FloatArray(3 * 640 * 640)
        YoloxContract.preprocess(frame, w, h, actual)
        assertEquals(expected.size, actual.size)
        var maxDiff = 0f
        var exact = 0
        for (i in expected.indices) {
            val d = abs(expected[i] - actual[i])
            if (d > maxDiff) maxDiff = d
            if (d == 0f) exact++
        }
        // The build machine's OpenCV resize is reproduced exactly for this frame size.
        assertEquals("largest difference $maxDiff", expected.size, exact)
    }

    @Test
    fun `decoding the full golden output gives the golden detections`() {
        val all = YoloxContract.decodeAllClasses(GoldenFixtures.floats("detector_output.f32"), goldenLetterbox(), 0.25f)
        assertSameDetections(GoldenFixtures.goldenDetections(), all, boxTol = 0f, scoreTol = 0f)
    }

    @Test
    fun `the crop cut from the frame is the golden crop`() {
        val (frame, w, _) = GoldenFixtures.png("frame.png")
        val (crop, cw, ch) = GoldenFixtures.png("crop.png")
        val px = GoldenFixtures.goldenCropPixels()
        assertEquals(px[2] - px[0], cw)
        assertEquals(px[3] - px[1], ch)
        assertArrayEquals(crop, CropRule.cut(frame, w, px))
    }

    @Test
    fun `the embedder input matches the golden embedder input`() {
        val (crop, w, h) = GoldenFixtures.png("crop.png")
        val expected = GoldenFixtures.floats("embedder_input.f32")
        val actual = FloatArray(3 * 224 * 224)
        ClipContract.preprocess(crop, w, h, actual)
        var maxDiff = 0f
        for (i in expected.indices) maxDiff = maxOf(maxDiff, abs(expected[i] - actual[i]))
        // Pillow's bicubic filter is reproduced bit for bit, so only float rounding may differ.
        assertTrue("largest difference $maxDiff", maxDiff <= 1e-5f)
    }
}
