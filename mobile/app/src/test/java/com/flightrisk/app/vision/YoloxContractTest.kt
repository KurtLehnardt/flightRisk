package com.flightrisk.app.vision

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The YOLOX-S contract of docs/mobile-models.md on synthetic tensors. */
class YoloxContractTest {

    private val plane = 640 * 640

    /** A [1, 8400, 85] output with every row zero except [rows]. */
    private fun output(vararg rows: Pair<Int, FloatArray>): FloatArray {
        val out = FloatArray(YoloxContract.NUM_ROWS * YoloxContract.ROW_LENGTH)
        for ((index, values) in rows) values.copyInto(out, index * YoloxContract.ROW_LENGTH)
        return out
    }

    private fun row(cx: Float, cy: Float, w: Float, h: Float, obj: Float, vararg classScores: Pair<Int, Float>): FloatArray {
        val r = FloatArray(YoloxContract.ROW_LENGTH)
        r[0] = cx; r[1] = cy; r[2] = w; r[3] = h; r[4] = obj
        for ((c, s) in classScores) r[5 + c] = s
        return r
    }

    private val shape = longArrayOf(1, 8400, 85)

    @Test
    fun `the ratio and resized size are truncated as upstream's preproc`() {
        assertEquals(Letterbox(0.8f, 640, 384, 0, 0), YoloxContract.scale(800, 480))
        assertEquals(Letterbox(0.5f, 640, 360, 0, 0), YoloxContract.scale(1280, 720))
        assertEquals(Letterbox(0.8f, 384, 640, 0, 0), YoloxContract.scale(480, 800))
        // 640 / 1001 * 1001 = 639.99... and is truncated, not rounded.
        val odd = YoloxContract.scale(1001, 333)
        assertEquals((640.0 / 1001 * 1001).toInt(), odd.resizedWidth)
        assertEquals((333 * (640.0 / 1001)).toInt(), odd.resizedHeight)
    }

    @Test
    fun `the canvas is BGR, 0 to 255, top-left, padded with 114`() {
        // A 10 x 8 frame of pure red (R=200, G=20, B=5), scaled by 64 to 640 x 512.
        val w = 10
        val h = 8
        val red = IntArray(w * h) { (0xFF shl 24) or (200 shl 16) or (20 shl 8) or 5 }
        val out = FloatArray(3 * plane)
        val lb = YoloxContract.preprocess(red, w, h, out)
        assertEquals(Letterbox(64f, 640, 512, 0, 0), lb)
        // Inside the resized frame: blue first, red last, no scaling.
        assertEquals(5f, out[0], 0f)
        assertEquals(20f, out[plane], 0f)
        assertEquals(200f, out[2 * plane], 0f)
        val lastInside = 511 * 640 + 639
        assertEquals(200f, out[2 * plane + lastInside], 0f)
        // Below it, the canvas is 114 in every channel: the frame is not centred.
        val firstPadded = 512 * 640
        for (c in 0 until 3) assertEquals(114f, out[c * plane + firstPadded], 0f)
        assertEquals(114f, out[2 * plane + plane - 1], 0f)
    }

    @Test
    fun `a row's score is objectness times its best class, and boxes are divided by the ratio`() {
        val lb = Letterbox(0.5f, 640, 360, 0, 0)
        val out = output(100 to row(100f, 50f, 20f, 40f, 0.9f, 0 to 0.8f))
        val dets = YoloxContract.decode(out, shape, lb, 0.25f)
        assertEquals(1, dets.size)
        val d = dets[0]
        assertEquals(0.9f * 0.8f, d.score, 0f)
        assertEquals(COCO_PERSON, d.classId)
        assertEquals(180f, d.x1, 1e-4f)
        assertEquals(60f, d.y1, 1e-4f)
        assertEquals(220f, d.x2, 1e-4f)
        assertEquals(140f, d.y2, 1e-4f)
    }

    @Test
    fun `a row whose best class is not a person is not a person, even with a high person score`() {
        val lb = Letterbox(1f, 640, 640, 0, 0)
        // Person 0.6, dog (16) 0.7: upstream takes the dog.
        val out = output(7 to row(100f, 100f, 20f, 40f, 1f, 0 to 0.6f, 16 to 0.7f))
        assertTrue(YoloxContract.decode(out, shape, lb, 0.25f).isEmpty())
        val all = YoloxContract.decodeAllClasses(out, lb, 0.25f)
        assertEquals(listOf(16), all.map { it.classId })
        // On equal scores the first class wins, as numpy's argmax does.
        val tie = output(7 to row(100f, 100f, 20f, 40f, 1f, 0 to 0.7f, 16 to 0.7f))
        assertEquals(listOf(COCO_PERSON), YoloxContract.decode(tie, shape, lb, 0.25f).map { it.classId })
    }

    @Test
    fun `rows under the threshold and NaN scores are dropped`() {
        val lb = Letterbox(1f, 640, 640, 0, 0)
        val out = output(
            1 to row(100f, 100f, 20f, 40f, 0.5f, 0 to 0.49f),
            2 to row(300f, 100f, 20f, 40f, Float.NaN, 0 to 0.9f),
            3 to row(500f, 100f, 20f, 40f, 0.5f, 0 to 0.5f),
        )
        val dets = YoloxContract.decode(out, shape, lb, 0.25f)
        assertEquals(1, dets.size)
        assertEquals(0.25f, dets[0].score, 0f)
    }

    @Test
    fun `NMS drops an overlap above 0_45 IoU within a class and keeps other classes`() {
        val lb = Letterbox(1f, 640, 640, 0, 0)
        val out = output(
            // Two people, IoU 0.6 (the second is shifted by 10 of 40 px): the weaker goes.
            10 to row(100f, 100f, 40f, 40f, 1f, 0 to 0.9f),
            11 to row(110f, 100f, 40f, 40f, 1f, 0 to 0.8f),
            // A third person beside them, IoU 0 with the first: kept.
            12 to row(300f, 100f, 40f, 40f, 1f, 0 to 0.7f),
            // A car (class 2) on top of the first person: another class, kept.
            13 to row(100f, 100f, 40f, 40f, 1f, 2 to 0.95f),
        )
        val all = YoloxContract.decodeAllClasses(out, lb, 0.25f)
        assertEquals(listOf(0.95f, 0.9f, 0.7f), all.map { it.score })
        assertEquals(listOf(2, 0, 0), all.map { it.classId })
        assertEquals(listOf(0.9f, 0.7f), YoloxContract.decode(out, shape, lb, 0.25f).map { it.score })
    }

    @Test
    fun `NMS keeps a box whose IoU is exactly at the threshold`() {
        val a = FrameBox(0f, 0f, 10f, 10f, 0.9f, 0)
        val iouOf = { b: FrameBox -> Nms.iou(a, b) }
        // Overlap 10 x 6 = 60; union 100 + 100 - 60 = 140; IoU 0.4286 <= 0.45.
        val b = FrameBox(0f, 4f, 10f, 14f, 0.8f, 0)
        assertTrue(iouOf(b) <= 0.45)
        assertEquals(listOf(0, 1), Nms.keep(listOf(a, b)))
        // Ties keep their input order.
        val c = FrameBox(100f, 100f, 110f, 110f, 0.9f, 0)
        assertEquals(listOf(0, 1), Nms.keep(listOf(a, c)))
    }

    @Test
    fun `the output shape is checked`() {
        val lb = Letterbox(1f, 640, 640, 0, 0)
        val bad = runCatching { YoloxContract.decode(FloatArray(84 * 8400), longArrayOf(1, 84, 8400), lb, 0.25f) }
        assertTrue(bad.isFailure)
    }

    @Test
    fun `the crop rule clips to the frame and truncates toward zero`() {
        assertArrayEquals(intArrayOf(0, 0, 10, 20), CropRule.pixels(FrameBox(-3.7f, -0.2f, 10.99f, 20.5f, 1f, 0), 100, 100))
        assertArrayEquals(intArrayOf(95, 90, 100, 100), CropRule.pixels(FrameBox(95.9f, 90.1f, 140f, 120f, 1f, 0), 100, 100))
        // A box entirely outside the frame, or thinner than a pixel, holds no pixel.
        assertNull(CropRule.pixels(FrameBox(120f, 10f, 140f, 30f, 1f, 0), 100, 100))
        assertNull(CropRule.pixels(FrameBox(10.2f, 10f, 10.9f, 30f, 1f, 0), 100, 100))
    }

    @Test
    fun `the crop is frame y1 to y2 and x1 to x2 with no margin`() {
        val w = 6
        val frame = IntArray(w * 4) { it }
        // Rows 1..2, columns 2..4.
        assertArrayEquals(intArrayOf(8, 9, 10, 14, 15, 16), CropRule.cut(frame, w, intArrayOf(2, 1, 5, 3)))
    }
}
