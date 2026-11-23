package com.flightrisk.app.vision

/**
 * The detector contract of `docs/mobile-models.md` ("Detector: YOLOX-S"), as
 * `scripts/mobile_models/mobile_preprocess.py` implements it: the model
 * `make_models.py install` copies into the assets as [ASSET].
 *
 * Input `images`, `[1, 3, 640, 640]`, BGR, values 0..255 (no scaling, no mean
 * or standard deviation). The frame keeps its aspect ratio, is resized
 * bilinearly to `int(width * r)` x `int(height * r)` with
 * `r = min(640 / height, 640 / width)`, and is placed at the top-left corner of
 * a canvas filled with 114.
 *
 * Output `detections`, `[1, 8400, 85]`: `cx, cy, w, h` in input pixels, then
 * objectness and 80 class scores (sigmoid applied). As upstream's
 * `yolox.utils.postprocess`: each row takes its best class, its score is
 * objectness times that class score, rows under the threshold are dropped and
 * non-maximum suppression runs per class at IoU 0.45. Boxes are divided by
 * `r` to get frame pixels.
 */
object YoloxContract : DetectorContract {
    const val ASSET = "yolox_s.onnx"
    const val INPUT_NAME = "images"
    const val OUTPUT_NAME = "detections"
    const val INPUT_SIZE = 640
    const val PAD_VALUE = 114
    const val NUM_CLASSES = 80
    const val ROW_LENGTH = 5 + NUM_CLASSES
    /** 80 * 80 + 40 * 40 + 20 * 20 rows at a 640 input (strides 8, 16 and 32). */
    const val NUM_ROWS = 8400

    override val inputSize: Int = INPUT_SIZE

    /** The resize ratio and resized width and height of a frame (upstream's `preproc`; truncated toward zero). */
    fun scale(width: Int, height: Int): Letterbox {
        require(width > 0 && height > 0) { "the image has no pixels" }
        val r = minOf(INPUT_SIZE.toDouble() / height, INPUT_SIZE.toDouble() / width)
        return Letterbox(
            ratio = r.toFloat(),
            resizedWidth = (width * r).toInt(),
            resizedHeight = (height * r).toInt(),
            padX = 0,
            padY = 0,
        )
    }

    override fun preprocess(argb: IntArray, width: Int, height: Int, out: FloatArray): Letterbox {
        val plane = INPUT_SIZE * INPUT_SIZE
        require(out.size >= 3 * plane) { "the input buffer is too small" }
        val lb = scale(width, height)
        val resized = ImageResize.bilinearCv(argb, width, height, lb.resizedWidth, lb.resizedHeight)
        out.fill(PAD_VALUE.toFloat(), 0, 3 * plane)
        for (y in 0 until lb.resizedHeight) {
            for (x in 0 until lb.resizedWidth) {
                val p = resized[y * lb.resizedWidth + x]
                val i = y * INPUT_SIZE + x
                // Channel 0 is blue and channel 2 is red: the model was trained on OpenCV's BGR.
                out[i] = (p and 0xFF).toFloat()
                out[plane + i] = ((p shr 8) and 0xFF).toFloat()
                out[2 * plane + i] = ((p shr 16) and 0xFF).toFloat()
            }
        }
        return lb
    }

    /** Every detection of every class at or above [scoreThreshold], as `mobile_preprocess.yolox_postprocess`. */
    fun decodeAllClasses(output: FloatArray, letterbox: Letterbox, scoreThreshold: Float): List<FrameBox> {
        require(output.size % ROW_LENGTH == 0) { "the output is not made of rows of $ROW_LENGTH values" }
        val rows = output.size / ROW_LENGTH
        val ratio = letterbox.ratio
        val candidates = ArrayList<FrameBox>()
        for (row in 0 until rows) {
            val o = row * ROW_LENGTH
            // argmax keeps the first of equal scores, as numpy's does.
            var best = 0
            var bestScore = output[o + 5]
            for (c in 1 until NUM_CLASSES) {
                val s = output[o + 5 + c]
                if (s > bestScore) {
                    bestScore = s
                    best = c
                }
            }
            val score = output[o + 4] * bestScore
            // A NaN score fails this test and is dropped.
            if (!(score >= scoreThreshold)) continue
            val cx = output[o]
            val cy = output[o + 1]
            val w = output[o + 2]
            val h = output[o + 3]
            candidates += FrameBox(
                x1 = (cx - w / 2f) / ratio,
                y1 = (cy - h / 2f) / ratio,
                x2 = (cx + w / 2f) / ratio,
                y2 = (cy + h / 2f) / ratio,
                score = score,
                classId = best,
            )
        }
        return Nms.perClass(candidates).sortedByDescending { it.score }
    }

    override fun decode(output: FloatArray, shape: LongArray, letterbox: Letterbox, scoreThreshold: Float): List<FrameBox> {
        require(shape.size == 3 && shape[0] == 1L && shape[2] == ROW_LENGTH.toLong()) {
            "expected a [1, N, $ROW_LENGTH] output, got ${shape.contentToString()}"
        }
        return decodeAllClasses(output, letterbox, scoreThreshold).filter { it.classId == COCO_PERSON }
    }
}
