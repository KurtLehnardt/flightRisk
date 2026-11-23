package com.flightrisk.app.vision

import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.round

/**
 * The two resize filters the model contracts name, on 8-bit pixels, written
 * out so the phone resizes the way the reference code does instead of the
 * way Android's bitmap scaler does. Pixels are ARGB ints (as
 * `Bitmap.getPixels` returns them, alpha ignored); results are packed RGB
 * ints with an opaque alpha.
 *
 * - [bilinearCv] is OpenCV's `cv2.resize(..., INTER_LINEAR)` on a uint8
 *   image, which upstream YOLOX's `preproc` (and Ultralytics' letterbox)
 *   use, as the pinned OpenCV 5.1 computes it (see there).
 * - [bicubicPillow] is Pillow's `Image.resize(..., BICUBIC)`, which
 *   torchvision and so open_clip use on the ground: a separable convolution
 *   with the a = -0.5 cubic kernel, widened when shrinking (antialiased),
 *   22-bit fixed-point weights and 8-bit rounding between the passes, in
 *   Pillow 13's pass order.
 *
 * The unit tests check both against the pinned libraries' output.
 */
internal object ImageResize {

    private const val CV_COEF_BITS = 11
    private const val CV_COEF_SCALE = 1 shl CV_COEF_BITS

    /** One output coordinate's first source index and its two 11-bit weights (the second index is clamped by the caller). */
    private class LinearTaps(val first: IntArray, val w0: IntArray, val w1: IntArray)

    private fun linearTaps(src: Int, dst: Int): LinearTaps {
        // OpenCV computes inv_scale = dst / src and scale = 1 / inv_scale, both in double.
        val scale = 1.0 / (dst.toDouble() / src.toDouble())
        val first = IntArray(dst)
        val w0 = IntArray(dst)
        val w1 = IntArray(dst)
        for (d in 0 until dst) {
            var f = ((d + 0.5) * scale - 0.5).toFloat()
            var s = floor(f.toDouble()).toInt()
            f -= s.toFloat()
            if (s < 0) {
                f = 0f
                s = 0
            }
            if (s >= src - 1) {
                f = 0f
                s = src - 1
            }
            first[d] = s
            // saturate_cast<short>(float) rounds half to even (cvRound).
            w0[d] = round(((1f - f) * CV_COEF_SCALE).toDouble()).toInt()
            w1[d] = round((f * CV_COEF_SCALE).toDouble()).toInt()
        }
        return LinearTaps(first, w0, w1)
    }

    /**
     * Resizes [argb] ([width] x [height]) to [newWidth] x [newHeight] as
     * OpenCV 5.1's `cv2.resize(..., INTER_LINEAR)` does on an 8-bit image (the
     * version `scripts/mobile_models/requirements.lock` pins, which made the
     * golden fixtures): OpenCV's weights (half-pixel centres, edge pixels
     * repeated, 11-bit fixed point), the rows resized first and rounded to 8
     * bits, then the columns, each pass rounding halves up; and, when both sides
     * halve exactly, the rounded mean of each 2 x 2 block.
     *
     * It matches that OpenCV exactly for the golden frame (800 x 480 to
     * 640 x 384), the camera's 1280 x 720 and 720 x 1280 frames, the Tello's
     * 960 x 720 frames and enlargements. OpenCV's output depends on its version
     * and build and, for some ratios, on the image size, so another OpenCV can
     * differ from this by a level or two, which the contract's tolerance covers.
     */
    fun bilinearCv(argb: IntArray, width: Int, height: Int, newWidth: Int, newHeight: Int): IntArray {
        require(width > 0 && height > 0 && newWidth > 0 && newHeight > 0) { "empty image" }
        require(argb.size >= width * height) { "too few pixels" }
        if (width == 2 * newWidth && height == 2 * newHeight) return areaHalf(argb, width, newWidth, newHeight)
        val xs = linearTaps(width, newWidth)
        val ys = linearTaps(height, newHeight)
        val half = 1 shl (CV_COEF_BITS - 1)
        fun mix(p0: Int, p1: Int, w0: Int, w1: Int): Int {
            val r = (((p0 shr 16) and 0xFF) * w0 + ((p1 shr 16) and 0xFF) * w1 + half) shr CV_COEF_BITS
            val g = (((p0 shr 8) and 0xFF) * w0 + ((p1 shr 8) and 0xFF) * w1 + half) shr CV_COEF_BITS
            val b = ((p0 and 0xFF) * w0 + (p1 and 0xFF) * w1 + half) shr CV_COEF_BITS
            return (0xFF shl 24) or (r shl 16) or (g shl 8) or b
        }
        // Rows first: every source row at the new width.
        val tmp = IntArray(height * newWidth)
        for (y in 0 until height) {
            val row = y * width
            for (dx in 0 until newWidth) {
                val sx = xs.first[dx]
                tmp[y * newWidth + dx] = mix(argb[row + sx], argb[row + minOf(sx + 1, width - 1)], xs.w0[dx], xs.w1[dx])
            }
        }
        // Then columns.
        val out = IntArray(newWidth * newHeight)
        for (dy in 0 until newHeight) {
            val r0 = ys.first[dy] * newWidth
            val r1 = minOf(ys.first[dy] + 1, height - 1) * newWidth
            val b0 = ys.w0[dy]
            val b1 = ys.w1[dy]
            for (dx in 0 until newWidth) out[dy * newWidth + dx] = mix(tmp[r0 + dx], tmp[r1 + dx], b0, b1)
        }
        return out
    }

    /** OpenCV's resize when both sides halve exactly: the rounded mean of each 2 x 2 block. */
    private fun areaHalf(argb: IntArray, width: Int, newWidth: Int, newHeight: Int): IntArray {
        val out = IntArray(newWidth * newHeight)
        for (dy in 0 until newHeight) {
            val r0 = 2 * dy * width
            val r1 = r0 + width
            for (dx in 0 until newWidth) {
                val a = argb[r0 + 2 * dx]
                val b = argb[r0 + 2 * dx + 1]
                val c = argb[r1 + 2 * dx]
                val d = argb[r1 + 2 * dx + 1]
                var px = 0xFF shl 24
                for (shift in intArrayOf(16, 8, 0)) {
                    val sum = ((a shr shift) and 0xFF) + ((b shr shift) and 0xFF) +
                        ((c shr shift) and 0xFF) + ((d shr shift) and 0xFF)
                    px = px or (((sum + 2) shr 2) shl shift)
                }
                out[dy * newWidth + dx] = px
            }
        }
        return out
    }

    // ------------------------------------------------------------------
    // Pillow bicubic
    // ------------------------------------------------------------------

    private const val PRECISION_BITS = 32 - 8 - 2
    private const val BICUBIC_SUPPORT = 2.0

    private fun bicubic(x0: Double): Double {
        val a = -0.5
        val x = if (x0 < 0.0) -x0 else x0
        if (x < 1.0) return ((a + 2.0) * x - (a + 3.0)) * x * x + 1
        if (x < 2.0) return (((x - 5) * x + 8) * x - 4) * a
        return 0.0
    }

    /** Pillow's `precompute_coeffs` and `normalize_coeffs_8bpc`: per output index, its first source index, tap count and fixed-point weights. */
    private class Coeffs(val ksize: Int, val bounds: IntArray, val weights: IntArray)

    private fun pillowCoeffs(inSize: Int, outSize: Int): Coeffs {
        val scale = inSize.toDouble() / outSize
        val filterScale = if (scale < 1.0) 1.0 else scale
        val support = BICUBIC_SUPPORT * filterScale
        val ksize = ceil(support).toInt() * 2 + 1
        val bounds = IntArray(outSize * 2)
        val weights = IntArray(outSize * ksize)
        val k = DoubleArray(ksize)
        for (xx in 0 until outSize) {
            val center = (xx + 0.5) * scale
            val ss = 1.0 / filterScale
            // C's (int) cast truncates toward zero.
            var xmin = (center - support + 0.5).toInt()
            if (xmin < 0) xmin = 0
            var xmax = (center + support + 0.5).toInt()
            if (xmax > inSize) xmax = inSize
            xmax -= xmin
            var ww = 0.0
            for (x in 0 until xmax) {
                val w = bicubic((x + xmin - center + 0.5) * ss)
                k[x] = w
                ww += w
            }
            for (x in 0 until ksize) {
                val v = if (x < xmax) (if (ww != 0.0) k[x] / ww else k[x]) else 0.0
                val scaled = v * (1 shl PRECISION_BITS)
                weights[xx * ksize + x] = if (v < 0) (-0.5 + scaled).toInt() else (0.5 + scaled).toInt()
            }
            bounds[xx * 2] = xmin
            bounds[xx * 2 + 1] = xmax
        }
        return Coeffs(ksize, bounds, weights)
    }

    private fun clip8(v: Int): Int = (v shr PRECISION_BITS).coerceIn(0, 255)

    /** Pillow's horizontal pass over rows [yFirst, yFirst + rows) of [src] ([srcWidth] wide). */
    private fun pillowHorizontal(src: IntArray, srcWidth: Int, yFirst: Int, rows: Int, newWidth: Int, c: Coeffs): IntArray {
        val out = IntArray(newWidth * rows)
        for (y in 0 until rows) {
            val base = (y + yFirst) * srcWidth
            for (xx in 0 until newWidth) {
                val xmin = c.bounds[xx * 2]
                val xmax = c.bounds[xx * 2 + 1]
                val kOff = xx * c.ksize
                var r = 1 shl (PRECISION_BITS - 1)
                var g = r
                var b = r
                for (x in 0 until xmax) {
                    val p = src[base + x + xmin]
                    val w = c.weights[kOff + x]
                    r += ((p shr 16) and 0xFF) * w
                    g += ((p shr 8) and 0xFF) * w
                    b += (p and 0xFF) * w
                }
                out[y * newWidth + xx] = (0xFF shl 24) or (clip8(r) shl 16) or (clip8(g) shl 8) or clip8(b)
            }
        }
        return out
    }

    /** Pillow's vertical pass: [newHeight] rows of [src] ([width] wide), row bounds shifted by [rowOffset]. */
    private fun pillowVertical(src: IntArray, width: Int, newHeight: Int, rowOffset: Int, c: Coeffs): IntArray {
        val out = IntArray(width * newHeight)
        for (yy in 0 until newHeight) {
            val ymin = c.bounds[yy * 2] - rowOffset
            val ymax = c.bounds[yy * 2 + 1]
            val kOff = yy * c.ksize
            for (xx in 0 until width) {
                var r = 1 shl (PRECISION_BITS - 1)
                var g = r
                var b = r
                for (y in 0 until ymax) {
                    val p = src[(y + ymin) * width + xx]
                    val w = c.weights[kOff + y]
                    r += ((p shr 16) and 0xFF) * w
                    g += ((p shr 8) and 0xFF) * w
                    b += (p and 0xFF) * w
                }
                out[yy * width + xx] = (0xFF shl 24) or (clip8(r) shl 16) or (clip8(g) shl 8) or clip8(b)
            }
        }
        return out
    }

    /**
     * Resizes [argb] ([width] x [height]) to [newWidth] x [newHeight] exactly
     * as Pillow 13's `Image.resize((newWidth, newHeight), Image.BICUBIC)` does
     * for an RGB image (the version `scripts/mobile_models/requirements.lock`
     * pins). Pillow resizes the rows first, unless the height shrinks by more
     * than twice as many pixels as the width, in which case the columns go
     * first; each pass rounds to 8 bits.
     */
    fun bicubicPillow(argb: IntArray, width: Int, height: Int, newWidth: Int, newHeight: Int): IntArray {
        require(width > 0 && height > 0 && newWidth > 0 && newHeight > 0) { "empty image" }
        require(argb.size >= width * height) { "too few pixels" }
        val needHorizontal = newWidth != width
        val needVertical = newHeight != height
        if (!needHorizontal && !needVertical) return argb.copyOf(width * height)
        val horizontalFirst = !((height - newHeight) > 0 && (height - newHeight) > (width - newWidth) * 2)
        val vert = pillowCoeffs(height, newHeight)
        val horiz = pillowCoeffs(width, newWidth)
        if (!horizontalFirst) {
            val tmp = if (needVertical) pillowVertical(argb, width, newHeight, 0, vert) else argb
            return if (needHorizontal) pillowHorizontal(tmp, width, 0, newHeight, newWidth, horiz) else tmp
        }
        if (!needHorizontal) return pillowVertical(argb, width, newHeight, 0, vert)
        // Only the rows the vertical pass reads are resized horizontally.
        val yFirst = vert.bounds[0]
        val yLast = vert.bounds[newHeight * 2 - 2] + vert.bounds[newHeight * 2 - 1]
        val tmp = pillowHorizontal(argb, width, yFirst, yLast - yFirst, newWidth, horiz)
        return if (needVertical) pillowVertical(tmp, newWidth, newHeight, yFirst, vert) else tmp
    }
}
