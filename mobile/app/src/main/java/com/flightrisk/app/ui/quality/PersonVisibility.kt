package com.flightrisk.app.ui.quality

import com.flightrisk.app.vision.FrameBox

/**
 * Whether a target photo shows one whole person large enough to match by
 * appearance. Matching compares how people look (clothing, colours, build),
 * not faces, so the photo needs the person's whole body, not their face.
 *
 * Pure function over the person detector's boxes (YOLOX-S), so it is tested on
 * the JVM; [TargetPhotoCheck] runs the detector on the phone.
 */
object PersonVisibility {
    /** A detection at or above this score counts as a person. */
    const val MIN_SCORE = 0.4f

    /** People smaller than this fraction of the photo height are background and are ignored. */
    const val MIN_RELEVANT_HEIGHT = 0.10f

    /** The person should be at least this fraction of the photo height ... */
    const val MIN_GOOD_HEIGHT = 0.40f

    /** ... and at least this many pixels tall. */
    const val MIN_GOOD_HEIGHT_PX = 100

    /** A box this close to the top or bottom edge (fraction of the height) is cut off by it. */
    const val EDGE_MARGIN = 0.01f

    const val NO_PERSON = "No person found"
    const val MORE_THAN_ONE = "More than one person"
    const val NOT_WHOLE = "Whole body not in the photo"
    const val TOO_SMALL = "Person is small in the photo"
    const val UNCHECKED = "Could not check the photo for a person (the detection model is not installed)"

    data class Result(val score: Float, val issues: List<String>, val suggestions: List<String>)

    /** The assessment of a [width] x [height] photo given the detector's person boxes, or of an unchecked photo when [persons] is null. */
    fun assess(persons: List<FrameBox>?, width: Int, height: Int): Result {
        if (persons == null) return Result(0.5f, listOf(UNCHECKED), emptyList())
        val relevant = persons.filter { it.score >= MIN_SCORE && (it.y2 - it.y1) >= MIN_RELEVANT_HEIGHT * height }
        if (relevant.isEmpty()) {
            return Result(0f, listOf(NO_PERSON), listOf("Use a photo that shows the person you are looking for"))
        }
        val issues = mutableListOf<String>()
        val suggestions = mutableListOf<String>()
        var score = 1f
        if (relevant.size > 1) {
            issues += MORE_THAN_ONE
            suggestions += "Use a photo of the missing person alone, or crop it so only they are in it"
            score -= 0.5f
        }
        val main = relevant.maxBy { it.score }
        val margin = EDGE_MARGIN * height
        if (main.y1 <= margin || main.y2 >= height - margin) {
            issues += NOT_WHOLE
            suggestions += "Use a photo that shows the person from head to feet: matching compares clothing and build, not faces"
            score -= 0.5f
        }
        val h = main.y2 - main.y1
        if (h < MIN_GOOD_HEIGHT * height || h < MIN_GOOD_HEIGHT_PX) {
            issues += TOO_SMALL
            suggestions += "Use a photo where the person fills more of the picture"
            score -= 0.3f
        }
        return Result(score.coerceAtLeast(0f), issues, suggestions)
    }
}
