package com.flightrisk.app.byo

/**
 * The check image an imported detector must find a person in before the app
 * uses it: a drawing, made in code, of person-like figures (a head, a torso,
 * two arms and two legs, 48 to 96 px tall) standing on a grass-like texture
 * with patches of bare ground, 640 x 480. No photograph is used and no image
 * file ships: the same seed always draws the same pixels.
 *
 * It follows the drawn figures of the golden fixtures
 * (`scripts/mobile_models/make_models.py`, `silhouette_scene`) with integer
 * arithmetic only. The seed was chosen so that YOLOX-S and a stock YOLO11n
 * export both find three people in it with scores of about 0.7 to 0.85
 * (the unit tests check the pixels, and YOLOX-S when the models are present).
 */
object CheckScene {
    const val WIDTH = 640
    const val HEIGHT = 480
    const val SEED = 29
    private val HEIGHTS = intArrayOf(48, 56, 64, 72, 80, 96)

    /** A 32-bit xorshift generator. */
    private class Rng(seed: Int) {
        private var s = if (seed != 0) seed else 0x9E3779B9.toInt()

        fun next(): Int {
            var x = s
            x = x xor (x shl 13)
            x = x xor (x ushr 17)
            x = x xor (x shl 5)
            s = x
            return x
        }

        /** A value in [lo, hi). */
        fun int(lo: Int, hi: Int): Int = lo + (next() ushr 1) % (hi - lo)
    }

    /** The scene as ARGB pixels, row-major. */
    fun pixels(seed: Int = SEED): IntArray {
        val rng = Rng(seed)
        val r = IntArray(WIDTH * HEIGHT)
        val g = IntArray(WIDTH * HEIGHT)
        val b = IntArray(WIDTH * HEIGHT)
        val base = intArrayOf(90, 120, 60)
        for ((c, plane) in listOf(r, g, b).withIndex()) {
            val gw = WIDTH / 4
            val gh = HEIGHT / 4
            val noise = IntArray(gw * gh) { rng.int(0, 256) }
            val up = IntArray(WIDTH * HEIGHT) { i -> noise[(i / WIDTH / 4) * gw + (i % WIDTH) / 4] }
            val texture = boxBlur(boxBlur(up, 4), 4)
            for (i in plane.indices) {
                plane[i] = (base[c] + Math.floorDiv((texture[i] - 128) * 45, 100)).coerceIn(0, 255)
            }
        }
        fun paint(i: Int, col: IntArray) {
            r[i] = col[0]
            g[i] = col[1]
            b[i] = col[2]
        }
        val ground = intArrayOf(150, 110, 70)
        repeat(12) {
            val cx = rng.int(0, WIDTH)
            val cy = rng.int(0, HEIGHT)
            val a = rng.int(20, 90).toLong()
            val bb = rng.int(8, 40).toLong()
            for (y in 0 until HEIGHT) for (x in 0 until WIDTH) {
                val dx = (x - cx).toLong()
                val dy = (y - cy).toLong()
                if (dx * dx * bb * bb + dy * dy * a * a <= a * a * bb * bb) paint(y * WIDTH + x, ground)
            }
        }
        fun disc(cx: Int, cy: Int, radius: Int, col: IntArray) {
            for (y in maxOf(0, cy - radius)..minOf(HEIGHT - 1, cy + radius)) {
                for (x in maxOf(0, cx - radius)..minOf(WIDTH - 1, cx + radius)) {
                    val dx = x - cx
                    val dy = y - cy
                    if (dx * dx + dy * dy <= radius * radius) paint(y * WIDTH + x, col)
                }
            }
        }
        fun line(x0: Int, y0: Int, x1: Int, y1: Int, thick: Int, col: IntArray) {
            val radius = maxOf(0, thick / 2)
            val steps = maxOf(kotlin.math.abs(x1 - x0), kotlin.math.abs(y1 - y0), 1)
            for (i in 0..steps) {
                disc(x0 + Math.floorDiv((x1 - x0) * i, steps), y0 + Math.floorDiv((y1 - y0) * i, steps), radius, col)
            }
        }
        fun rect(x0: Int, y0: Int, x1: Int, y1: Int, col: IntArray) {
            for (y in maxOf(0, y0)..minOf(HEIGHT - 1, y1)) for (x in maxOf(0, x0)..minOf(WIDTH - 1, x1)) paint(y * WIDTH + x, col)
        }
        var x = 20
        for (fh in HEIGHTS) {
            val fw = maxOf(4, (fh * 4 + 5) / 10)
            val yTop = rng.int(10, HEIGHT - fh - 10)
            if (x + fw >= WIDTH - 10) x = 20
            val cx = x + fw / 2
            val skin = IntArray(3) { rng.int(60, 200) }
            val shirt = IntArray(3) { rng.int(0, 256) }
            val trousers = IntArray(3) { rng.int(0, 140) }
            val headR = maxOf(1, (fh * 65 + 500) / 1000)
            val thick = maxOf(1, (fh * 7 + 50) / 100)
            val neck = yTop + 2 * headR
            val hip = yTop + (fh * 52) / 100
            val sh = (fh * 11) / 100
            disc(cx, yTop + headR, headR, skin)
            rect(cx - sh, neck, cx + sh, hip, shirt)
            line(cx - sh, neck + thick, cx - (fh * 18) / 100, yTop + (fh * 50) / 100, thick, shirt)
            line(cx + sh, neck + thick, cx + (fh * 18) / 100, yTop + (fh * 50) / 100, thick, shirt)
            line(cx - (fh * 6) / 100, hip, cx - (fh * 9) / 100, yTop + fh - 1, thick, trousers)
            line(cx + (fh * 6) / 100, hip, cx + (fh * 9) / 100, yTop + fh - 1, thick, trousers)
            x += fw + rng.int(20, 60)
        }
        return IntArray(WIDTH * HEIGHT) { i -> (0xFF shl 24) or (r[i] shl 16) or (g[i] shl 8) or b[i] }
    }

    /** The mean over a (2 * radius + 1) window, rows first, then columns, with edge pixels repeated. */
    private fun boxBlur(a: IntArray, radius: Int): IntArray {
        val n = 2 * radius + 1
        val rows = IntArray(a.size)
        for (y in 0 until HEIGHT) for (x in 0 until WIDTH) {
            var s = 0
            for (k in -radius..radius) s += a[y * WIDTH + (x + k).coerceIn(0, WIDTH - 1)]
            rows[y * WIDTH + x] = s / n
        }
        val out = IntArray(a.size)
        for (y in 0 until HEIGHT) for (x in 0 until WIDTH) {
            var s = 0
            for (k in -radius..radius) s += rows[(y + k).coerceIn(0, HEIGHT - 1) * WIDTH + x]
            out[y * WIDTH + x] = s / n
        }
        return out
    }
}
