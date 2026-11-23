package com.flightrisk.app.vision

import kotlin.math.round
import kotlin.math.sqrt

/**
 * The embedder contract of `docs/mobile-models.md` ("Appearance embedder:
 * open_clip ViT-B-16 `laion2b_s34b_b88k`"), as
 * `scripts/mobile_models/mobile_preprocess.py` implements it, for the model
 * `make_models.py install` copies into the assets as [ASSET]. It is the
 * ground's own model (`open_clip:ViT-B-16:laion2b_s34b_b88k`).
 *
 * Input `input`, `[1, 3, 224, 224]`, RGB. The person crop's shortest side is
 * resized to 224 with Pillow's bicubic filter (the long side becomes
 * `int(224 * long / short)`), the centre 224 x 224 is cut out (offsets
 * `round((side - 224) / 2)`, halves to even), and each value becomes
 * `(v / 255 - mean) / std`.
 *
 * Output `embedding`, `[1, 512]`, L2-normalised by the model.
 */
object ClipContract {
    const val ASSET = "clip_vit_b16_laion2b.onnx"
    const val INPUT_NAME = "input"
    const val OUTPUT_NAME = "embedding"
    const val INPUT_SIZE = 224
    const val EMBEDDING_DIM = 512

    val MEAN = floatArrayOf(0.48145466f, 0.4578275f, 0.40821073f)
    val STD = floatArrayOf(0.26862954f, 0.26130258f, 0.27577711f)

    val INPUT_SHAPE = longArrayOf(1, 3, INPUT_SIZE.toLong(), INPUT_SIZE.toLong())

    /** (width, height) after resizing the shortest side to 224 (torchvision's `Resize(224)`). */
    fun resizedSize(width: Int, height: Int): Pair<Int, Int> {
        require(width > 0 && height > 0) { "the image has no pixels" }
        return if (width <= height) {
            INPUT_SIZE to (INPUT_SIZE.toDouble() * height / width).toInt()
        } else {
            (INPUT_SIZE.toDouble() * width / height).toInt() to INPUT_SIZE
        }
    }

    /** (left, top) of the centre crop of a [width] x [height] image, as torchvision's `center_crop`. */
    fun cropOffsets(width: Int, height: Int): Pair<Int, Int> =
        round((width - INPUT_SIZE) / 2.0).toInt() to round((height - INPUT_SIZE) / 2.0).toInt()

    /** The 224 x 224 RGB pixels the embedder sees for a crop of [width] x [height] ARGB pixels. */
    fun resizeAndCrop(argb: IntArray, width: Int, height: Int): IntArray {
        val (newW, newH) = resizedSize(width, height)
        val resized = if (newW == width && newH == height) {
            argb
        } else {
            ImageResize.bicubicPillow(argb, width, height, newW, newH)
        }
        val (left, top) = cropOffsets(newW, newH)
        val out = IntArray(INPUT_SIZE * INPUT_SIZE)
        for (y in 0 until INPUT_SIZE) {
            System.arraycopy(resized, (y + top) * newW + left, out, y * INPUT_SIZE, INPUT_SIZE)
        }
        return out
    }

    /** Writes the `[1, 3, 224, 224]` network input of a crop into [out] (3 * 224 * 224 values). */
    fun preprocess(argb: IntArray, width: Int, height: Int, out: FloatArray) {
        val plane = INPUT_SIZE * INPUT_SIZE
        require(out.size >= 3 * plane) { "the input buffer is too small" }
        val square = resizeAndCrop(argb, width, height)
        for (i in 0 until plane) {
            val p = square[i]
            out[i] = (((p shr 16) and 0xFF) / 255f - MEAN[0]) / STD[0]
            out[plane + i] = (((p shr 8) and 0xFF) / 255f - MEAN[1]) / STD[1]
            out[2 * plane + i] = ((p and 0xFF) / 255f - MEAN[2]) / STD[2]
        }
    }

    /** [v] divided by its L2 norm (in double precision); a zero vector stays zero. */
    fun l2Normalize(v: FloatArray): FloatArray {
        var sum = 0.0
        for (x in v) sum += x.toDouble() * x
        val norm = sqrt(sum)
        return if (norm > 0.0) FloatArray(v.size) { (v[it] / norm).toFloat() } else v.copyOf()
    }

    /** Cosine similarity of two vectors, in double precision. */
    fun cosine(a: FloatArray, b: FloatArray): Double {
        require(a.size == b.size) { "the vectors differ in length" }
        var dot = 0.0
        var na = 0.0
        var nb = 0.0
        for (i in a.indices) {
            dot += a[i].toDouble() * b[i]
            na += a[i].toDouble() * a[i]
            nb += b[i].toDouble() * b[i]
        }
        return if (na > 0.0 && nb > 0.0) dot / (sqrt(na) * sqrt(nb)) else 0.0
    }
}
