package com.flightrisk.app.vision

/**
 * How a camera frame was placed in a detector's square input: the resize
 * ratio, the size of the resized frame and the padding before it (zero for
 * YOLOX, which places the frame at the top-left corner).
 */
data class Letterbox(
    val ratio: Float,
    val resizedWidth: Int,
    val resizedHeight: Int,
    val padX: Int,
    val padY: Int,
)

/**
 * One detection in frame pixels, as the model reported it: not clipped to the
 * frame and not rounded. [CropRule.pixels] turns it into the integer box the
 * crop is cut at.
 */
data class FrameBox(
    val x1: Float,
    val y1: Float,
    val x2: Float,
    val y2: Float,
    val score: Float,
    val classId: Int,
)

/**
 * The pre- and postprocessing of one detector model family. Implementations
 * are pure functions (no Android types), so they are tested on the JVM with
 * synthetic tensors and with the golden fixtures of `make_models.py verify`.
 */
interface DetectorContract {
    /** Side of the square network input, in pixels. */
    val inputSize: Int

    /** The `[1, 3, inputSize, inputSize]` shape of the network input. */
    val inputShape: LongArray
        get() = longArrayOf(1, 3, inputSize.toLong(), inputSize.toLong())

    /**
     * Writes the network input for a frame of [width] x [height] ARGB pixels
     * ([argb], row-major, as `Bitmap.getPixels` returns them) into [out]
     * (`3 * inputSize * inputSize` values, NCHW) and returns where the frame
     * was placed.
     */
    fun preprocess(argb: IntArray, width: Int, height: Int, out: FloatArray): Letterbox

    /**
     * The person detections (COCO class 0) at or above [scoreThreshold] in the
     * network output [output] of shape [shape], in frame pixels, after
     * non-maximum suppression, highest score first.
     */
    fun decode(output: FloatArray, shape: LongArray, letterbox: Letterbox, scoreThreshold: Float): List<FrameBox>
}

/** COCO class index of "person", the only class the app keeps. */
const val COCO_PERSON = 0

/** Non-maximum suppression IoU of upstream YOLOX's `postprocess` (per class). */
const val DETECTOR_NMS_IOU = 0.45f

/**
 * Greedy non-maximum suppression, as `scripts/mobile_models/mobile_preprocess.py`
 * (`_nms`) does it: boxes are taken by score, highest first (ties keep their
 * input order), and a box is dropped when its IoU with a kept box is above
 * [iouThreshold]. IoU is computed in double precision. Returns the indices kept.
 */
object Nms {
    fun keep(boxes: List<FrameBox>, iouThreshold: Float = DETECTOR_NMS_IOU): List<Int> {
        if (boxes.isEmpty()) return emptyList()
        // sortedByDescending is stable, like numpy's argsort(-scores, kind="stable").
        val order = boxes.indices.sortedByDescending { boxes[it].score }
        val suppressed = BooleanArray(boxes.size)
        val kept = mutableListOf<Int>()
        for (n in order.indices) {
            val i = order[n]
            if (suppressed[i]) continue
            kept += i
            for (m in n + 1 until order.size) {
                val j = order[m]
                if (!suppressed[j] && iou(boxes[i], boxes[j]) > iouThreshold) suppressed[j] = true
            }
        }
        return kept
    }

    /** Intersection over union of two corner boxes; zero when the union is empty. */
    fun iou(a: FrameBox, b: FrameBox): Double {
        val w = minOf(a.x2.toDouble(), b.x2.toDouble()) - maxOf(a.x1.toDouble(), b.x1.toDouble())
        val h = minOf(a.y2.toDouble(), b.y2.toDouble()) - maxOf(a.y1.toDouble(), b.y1.toDouble())
        val inter = maxOf(w, 0.0) * maxOf(h, 0.0)
        val areaA = maxOf(a.x2.toDouble() - a.x1, 0.0) * maxOf(a.y2.toDouble() - a.y1, 0.0)
        val areaB = maxOf(b.x2.toDouble() - b.x1, 0.0) * maxOf(b.y2.toDouble() - b.y1, 0.0)
        val union = areaA + areaB - inter
        return if (union > 0.0) inter / union else 0.0
    }

    /** At most this many candidates (the highest scores) go into suppression, so a hostile or noisy output cannot make it quadratic in 8400 rows (NMS sees only the top 300). */
    const val MAX_CANDIDATES = 300

    /** At most this many detections come out of a frame. */
    const val MAX_DETECTIONS = 300

    /**
     * Runs [keep] separately for each class, as upstream YOLOX does, on the
     * [MAX_CANDIDATES] highest-scoring boxes, and returns at most
     * [MAX_DETECTIONS] kept boxes, highest score first.
     */
    fun perClass(boxes: List<FrameBox>, iouThreshold: Float = DETECTOR_NMS_IOU): List<FrameBox> {
        val candidates = if (boxes.size > MAX_CANDIDATES) boxes.sortedByDescending { it.score }.take(MAX_CANDIDATES) else boxes
        return candidates.groupBy { it.classId }.values
            .flatMap { group -> keep(group, iouThreshold).map { group[it] } }
            .sortedByDescending { it.score }
            .take(MAX_DETECTIONS)
    }
}

/**
 * The crop rule of `docs/mobile-models.md` ("From a detection to the embedder's
 * crop"), as the ground does it: the box in frame pixels is clipped to the
 * frame, each corner is truncated toward zero, and the crop is
 * `frame[y1:y2, x1:x2]` with no margin.
 */
object CropRule {
    /** The integer `x1, y1, x2, y2` the frame is cut at, or null when the box holds no pixel. */
    fun pixels(box: FrameBox, width: Int, height: Int): IntArray? {
        val x1 = clip(box.x1, width)
        val y1 = clip(box.y1, height)
        val x2 = clip(box.x2, width)
        val y2 = clip(box.y2, height)
        return if (x2 > x1 && y2 > y1) intArrayOf(x1, y1, x2, y2) else null
    }

    // Kotlin's toInt() truncates toward zero, like Python's int(). The value is
    // first widened to a double, as Python's float() does with a float32.
    private fun clip(v: Float, limit: Int): Int = v.toDouble().coerceIn(0.0, limit.toDouble()).toInt()

    /** The [width] x [height] ARGB pixels of the crop at [box] (from [pixels]) of a frame [frameWidth] wide. */
    fun cut(argb: IntArray, frameWidth: Int, box: IntArray): IntArray {
        val (x1, y1, x2, y2) = box
        val w = x2 - x1
        val out = IntArray(w * (y2 - y1))
        for (y in y1 until y2) System.arraycopy(argb, y * frameWidth + x1, out, (y - y1) * w, w)
        return out
    }
}
