package com.flightrisk.app.vision

import kotlin.math.round

/**
 * Pre- and postprocessing for a person detector the user imports themselves
 * (Settings, "Bring your own detector"). FlightRisk does not supply, bundle
 * or license any such model; see [com.flightrisk.app.byo.ByoDetectorChecks].
 *
 * Input `[1, 3, 640, 640]`, RGB, values 0..1. The frame keeps its aspect
 * ratio, is resized bilinearly to `round(width * r)` x `round(height * r)`
 * with `r = min(640 / height, 640 / width)` and is centred on a canvas filled
 * with 114, the padding split as `round(d / 2 - 0.1)` before and the rest
 * after (the common letterbox of these exports).
 *
 * Two output layouts are accepted:
 * - [Layout.ANCHORS]: `[1, 84, N]`, one column per anchor: `cx, cy, w, h` in
 *   input pixels, then 80 class scores. Each anchor takes its best class;
 *   non-maximum suppression then runs per class.
 * - [Layout.END_TO_END]: `[1, 300, 6]`, rows of `x1, y1, x2, y2, score,
 *   class` in input pixels, already suppressed.
 *
 * Person is class 0, as in COCO.
 *
 * Suppression uses the app's IoU of 0.45 (as for YOLOX-S), not the 0.7 that
 * Ultralytics' own `predict` uses by default: 0.45 removes more overlapping
 * boxes, so two people standing close together are more often reported as
 * one. End-to-end exports are already suppressed by the model.
 */
class UltralyticsContract(val layout: Layout) : DetectorContract {

    enum class Layout { ANCHORS, END_TO_END }

    companion object {
        const val INPUT_SIZE = 640
        const val PAD_VALUE = 114
        const val NUM_CLASSES = 80
        const val ANCHOR_CHANNELS = 4 + NUM_CLASSES
        const val END_TO_END_ROWS = 300
        const val END_TO_END_VALUES = 6

        /** The layout of an output of [shape], or null when it is neither accepted layout. */
        fun layoutOf(shape: LongArray): Layout? = when {
            shape.size == 3 && shape[0] == 1L && shape[1] == ANCHOR_CHANNELS.toLong() && shape[2] > 0L -> Layout.ANCHORS
            shape.size == 3 && shape[0] == 1L && shape[1] == END_TO_END_ROWS.toLong() &&
                shape[2] == END_TO_END_VALUES.toLong() -> Layout.END_TO_END
            else -> null
        }

        /** Python's `round`: halves go to the even neighbour. */
        private fun pyRound(v: Double): Int = round(v).toInt()

        /** Where a [width] x [height] frame is placed in the input. */
        fun letterbox(width: Int, height: Int): Letterbox {
            require(width > 0 && height > 0) { "the image has no pixels" }
            val r = minOf(INPUT_SIZE.toDouble() / height, INPUT_SIZE.toDouble() / width)
            val newW = pyRound(width * r)
            val newH = pyRound(height * r)
            val dw = (INPUT_SIZE - newW) / 2.0
            val dh = (INPUT_SIZE - newH) / 2.0
            return Letterbox(
                ratio = r.toFloat(),
                resizedWidth = newW,
                resizedHeight = newH,
                padX = pyRound(dw - 0.1),
                padY = pyRound(dh - 0.1),
            )
        }
    }

    override val inputSize: Int = INPUT_SIZE

    override fun preprocess(argb: IntArray, width: Int, height: Int, out: FloatArray): Letterbox {
        val plane = INPUT_SIZE * INPUT_SIZE
        require(out.size >= 3 * plane) { "the input buffer is too small" }
        val lb = letterbox(width, height)
        val resized = if (lb.resizedWidth == width && lb.resizedHeight == height) {
            argb
        } else {
            ImageResize.bilinearCv(argb, width, height, lb.resizedWidth, lb.resizedHeight)
        }
        out.fill(PAD_VALUE / 255f, 0, 3 * plane)
        for (y in 0 until lb.resizedHeight) {
            val ty = y + lb.padY
            if (ty !in 0 until INPUT_SIZE) continue
            for (x in 0 until lb.resizedWidth) {
                val tx = x + lb.padX
                if (tx !in 0 until INPUT_SIZE) continue
                val p = resized[y * lb.resizedWidth + x]
                val i = ty * INPUT_SIZE + tx
                out[i] = ((p shr 16) and 0xFF) / 255f
                out[plane + i] = ((p shr 8) and 0xFF) / 255f
                out[2 * plane + i] = (p and 0xFF) / 255f
            }
        }
        return lb
    }

    private fun toFrame(x1: Float, y1: Float, x2: Float, y2: Float, score: Float, cls: Int, lb: Letterbox) = FrameBox(
        x1 = (x1 - lb.padX) / lb.ratio,
        y1 = (y1 - lb.padY) / lb.ratio,
        x2 = (x2 - lb.padX) / lb.ratio,
        y2 = (y2 - lb.padY) / lb.ratio,
        score = score,
        classId = cls,
    )

    override fun decode(output: FloatArray, shape: LongArray, letterbox: Letterbox, scoreThreshold: Float): List<FrameBox> {
        val actual = layoutOf(shape)
        require(actual == layout) { "expected the ${layout.name} layout, got ${shape.contentToString()}" }
        return when (layout) {
            Layout.ANCHORS -> decodeAnchors(output, shape[2].toInt(), letterbox, scoreThreshold)
            Layout.END_TO_END -> decodeEndToEnd(output, letterbox, scoreThreshold)
        }
    }

    private fun decodeAnchors(output: FloatArray, anchors: Int, lb: Letterbox, threshold: Float): List<FrameBox> {
        require(output.size == ANCHOR_CHANNELS * anchors) { "the output has ${output.size} values, not $ANCHOR_CHANNELS x $anchors" }
        val candidates = ArrayList<FrameBox>()
        for (n in 0 until anchors) {
            var best = 0
            var bestScore = output[4 * anchors + n]
            for (c in 1 until NUM_CLASSES) {
                val s = output[(4 + c) * anchors + n]
                if (s > bestScore) {
                    bestScore = s
                    best = c
                }
            }
            if (best != COCO_PERSON || !(bestScore >= threshold)) continue
            val cx = output[n]
            val cy = output[anchors + n]
            val w = output[2 * anchors + n]
            val h = output[3 * anchors + n]
            candidates += toFrame(cx - w / 2f, cy - h / 2f, cx + w / 2f, cy + h / 2f, bestScore, best, lb)
        }
        return Nms.perClass(candidates).sortedByDescending { it.score }
    }

    private fun decodeEndToEnd(output: FloatArray, lb: Letterbox, threshold: Float): List<FrameBox> {
        require(output.size == END_TO_END_ROWS * END_TO_END_VALUES) { "the output has ${output.size} values" }
        val out = ArrayList<FrameBox>()
        for (row in 0 until END_TO_END_ROWS) {
            val o = row * END_TO_END_VALUES
            val score = output[o + 4]
            val cls = output[o + 5]
            if (!(score >= threshold) || cls.isNaN() || round(cls.toDouble()).toInt() != COCO_PERSON) continue
            out += toFrame(output[o], output[o + 1], output[o + 2], output[o + 3], score, COCO_PERSON, lb)
        }
        return out.sortedByDescending { it.score }
    }
}
