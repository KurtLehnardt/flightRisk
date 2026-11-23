package com.flightrisk.app.demo

/**
 * The pictures demo mode uses instead of photos: a placeholder target and a drawn search scene. Both
 * are flat shapes in a handful of solid colours, computed here pixel by pixel, so no image of a real
 * person is ever shipped, stored or shown. [DemoImages] turns them into bitmaps on a device; this
 * object is plain Kotlin so its output can be checked in JVM tests.
 */
object DemoArt {
    const val TARGET_WIDTH = 240
    const val TARGET_HEIGHT = 320
    const val SCENE_WIDTH = 640
    const val SCENE_HEIGHT = 480

    // Solid colours (ARGB). Nothing here is sampled from a photo.
    internal const val BACKDROP = 0xFFE3E8EF.toInt()
    internal const val STRIPE = 0xFFFFB300.toInt()
    internal const val HEAD = 0xFF8D6E63.toInt()
    internal const val JACKET = 0xFFD32F2F.toInt()
    internal const val TROUSERS = 0xFF1E3A8A.toInt()
    internal const val BACKPACK = 0xFF2E7D32.toInt()
    internal const val GRASS = 0xFF7CB342.toInt()
    internal const val PATH = 0xFFD7CCC8.toInt()
    internal const val TREE = 0xFF33691E.toInt()

    /** Every colour the pictures may contain. A photo would have thousands. */
    val PALETTE: Set<Int> = setOf(BACKDROP, STRIPE, HEAD, JACKET, TROUSERS, BACKPACK, GRASS, PATH, TREE)

    /**
     * Where the figure stands in [scenePixels], as `[x1, y1, x2, y2]` in scene pixels: the demo's one
     * detection box.
     */
    val SCENE_FIGURE_BOX: IntArray = intArrayOf(388, 214, 428, 300)

    /** The placeholder target: a drawn figure (red jacket, blue trousers, green backpack) on a striped card. */
    fun targetPixels(): IntArray {
        val w = TARGET_WIDTH
        val h = TARGET_HEIGHT
        val px = IntArray(w * h) { BACKDROP }
        // Diagonal stripes in two corners mark the picture as a placeholder.
        for (y in 0 until h) for (x in 0 until w) {
            val d = x + y
            val e = (w - 1 - x) + (h - 1 - y)
            if ((d < 60 && (d / 12) % 2 == 0) || (e < 60 && (e / 12) % 2 == 0)) px[y * w + x] = STRIPE
        }
        drawFigure(px, w, cx = w / 2, top = 40, scale = 1.0)
        return px
    }

    /** The demo search scene: a park seen from a drone, a path, trees and one small figure. */
    fun scenePixels(): IntArray {
        val w = SCENE_WIDTH
        val h = SCENE_HEIGHT
        val px = IntArray(w * h) { GRASS }
        // A path across the park.
        for (y in 0 until h) for (x in 0 until w) {
            val onPath = kotlin.math.abs((y - 120) - (x - 80) * 0.45) < 18
            if (onPath) px[y * w + x] = PATH
        }
        for ((cx, cy, r) in listOf(Triple(90, 380, 46), Triple(200, 70, 38), Triple(560, 400, 52), Triple(560, 90, 34))) {
            fillCircle(px, w, h, cx, cy, r, TREE)
        }
        val box = SCENE_FIGURE_BOX
        drawFigure(px, w, cx = (box[0] + box[2]) / 2, top = box[1], scale = 0.36, height = h)
        return px
    }

    private fun drawFigure(px: IntArray, w: Int, cx: Int, top: Int, scale: Double, height: Int = TARGET_HEIGHT) {
        fun s(v: Int) = (v * scale).toInt()
        val h = height
        // Backpack behind the body, then the body, the legs and the head.
        fillRect(px, w, h, cx - s(46), top + s(70), cx + s(46), top + s(150), BACKPACK)
        fillRect(px, w, h, cx - s(36), top + s(62), cx + s(36), top + s(160), JACKET)
        fillRect(px, w, h, cx - s(30), top + s(160), cx - s(4), top + s(236), TROUSERS)
        fillRect(px, w, h, cx + s(4), top + s(160), cx + s(30), top + s(236), TROUSERS)
        fillCircle(px, w, h, cx, top + s(32), s(28).coerceAtLeast(2), HEAD)
    }

    private fun fillRect(px: IntArray, w: Int, h: Int, x1: Int, y1: Int, x2: Int, y2: Int, color: Int) {
        for (y in y1.coerceAtLeast(0) until y2.coerceAtMost(h)) {
            for (x in x1.coerceAtLeast(0) until x2.coerceAtMost(w)) px[y * w + x] = color
        }
    }

    private fun fillCircle(px: IntArray, w: Int, h: Int, cx: Int, cy: Int, r: Int, color: Int) {
        for (y in (cy - r).coerceAtLeast(0)..(cy + r).coerceAtMost(h - 1)) {
            for (x in (cx - r).coerceAtLeast(0)..(cx + r).coerceAtMost(w - 1)) {
                val dx = x - cx
                val dy = y - cy
                if (dx * dx + dy * dy <= r * r) px[y * w + x] = color
            }
        }
    }
}
