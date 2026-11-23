package com.flightrisk.app.vision

/**
 * Drops detections whose box is too small, or whose shape is too far from a
 * person's, to be worth matching. The box is the integer crop box in frame
 * pixels.
 *
 * The phone used to require a box at least 12% of the frame height and 0.8%
 * of its area ([LEGACY]). From a drone a person is often 1 to 5% of the frame
 * height (16 to 48 px tall in a 720p or 1080p frame), so those values threw
 * away exactly the people an aerial search is for. [AERIAL], the default,
 * keeps a person down to about 1% of the frame height and allows wide boxes
 * too, because a person seen from above, or lying down, can be wider than
 * tall.
 *
 * @property minHeightRatio Smallest box height, as a fraction of the frame height.
 * @property minAreaRatio Smallest box area, as a fraction of the frame area.
 * @property minAspectRatio Smallest width / height.
 * @property maxAspectRatio Largest width / height.
 */
data class DetectionSizeFilter(
    val minHeightRatio: Float,
    val minAreaRatio: Float,
    val minAspectRatio: Float,
    val maxAspectRatio: Float,
) {
    init {
        require(minHeightRatio in 0f..1f && minAreaRatio in 0f..1f) { "ratios must be between 0 and 1" }
        require(minAspectRatio >= 0f && maxAspectRatio >= minAspectRatio) { "invalid aspect ratio range" }
    }

    /** True when a box [box] (`x1, y1, x2, y2`) in a [frameWidth] x [frameHeight] frame is kept. */
    fun keeps(box: IntArray, frameWidth: Int, frameHeight: Int): Boolean {
        val w = (box[2] - box[0]).toFloat()
        val h = (box[3] - box[1]).toFloat()
        if (w <= 0f || h <= 0f) return false
        val aspect = w / h
        return h >= frameHeight * minHeightRatio &&
            w * h >= frameWidth.toFloat() * frameHeight * minAreaRatio &&
            aspect >= minAspectRatio &&
            aspect <= maxAspectRatio
    }

    companion object {
        /**
         * The default: a box at least 1% of the frame height (7 px at 720p)
         * and 0.005% of its area (about 7 x 7 px at 720p), and between 1:10
         * and 4:1 wide.
         */
        val AERIAL = DetectionSizeFilter(
            minHeightRatio = 0.01f,
            minAreaRatio = 0.00005f,
            minAspectRatio = 0.1f,
            maxAspectRatio = 4.0f,
        )

        /** The filter the phone used before: 12% of the height, 0.8% of the area, 0.15 to 1.5 wide. */
        val LEGACY = DetectionSizeFilter(
            minHeightRatio = 0.12f,
            minAreaRatio = 0.008f,
            minAspectRatio = 0.15f,
            maxAspectRatio = 1.5f,
        )

        /** No filtering at all. */
        val NONE = DetectionSizeFilter(0f, 0f, 0f, Float.MAX_VALUE)
    }
}
