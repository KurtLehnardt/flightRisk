package com.flightrisk.app.vision

/**
 * Which detections of a frame the appearance matcher compares with the target.
 *
 * Detection keeps people down to about 1% of the frame height, but an
 * appearance embedding of a crop only a few dozen pixels tall says little:
 * below about 24 px someone else's crop scores as high against the target as
 * the same person's. So only crops at least [minCropHeightPx] tall are
 * compared, and only the [maxPerFrame] most confident of those, which also
 * bounds the embedder's work per frame. Smaller people are still detected,
 * tracked and sent to a ground station; they just get no appearance score.
 */
data class MatchSelection(val minCropHeightPx: Int, val maxPerFrame: Int) {
    init {
        require(minCropHeightPx >= 1 && maxPerFrame >= 0) { "invalid match selection" }
    }

    /**
     * The indices of the detections to compare, given each detection's crop
     * height in pixels and its detector score: the tall-enough ones, most
     * confident first (ties keep their order), at most [maxPerFrame].
     */
    fun eligible(cropHeights: List<Int>, confidences: List<Float>): List<Int> {
        require(cropHeights.size == confidences.size) { "one height and one score per detection" }
        return cropHeights.indices
            .filter { cropHeights[it] >= minCropHeightPx }
            .sortedByDescending { confidences[it] }
            .take(maxPerFrame)
    }

    companion object {
        /** From the app configuration (40 px and 5 per frame by default). */
        fun from(config: com.flightrisk.app.config.PhoneVisionConfig) =
            MatchSelection(config.matchMinCropHeightPx, config.matchMaxPerFrame)
    }
}
