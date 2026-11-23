package com.flightrisk.app.ui.search

import com.flightrisk.app.drone.FrameSourceMode

/**
 * Whether a search can start, and what the Search screen should say before it
 * does. Only [NO_CAMERA] stops a search from starting.
 */
enum class SearchReadiness(
    /** Whether Start may begin a search in this state. */
    val canStart: Boolean,
) {
    /** A target photo is set and there is a camera to search with. */
    READY(canStart = true),

    /**
     * No target photo: onboarding was finished with "Set up a search later" or
     * the app was restarted (the photo is kept in memory only). A search can
     * still start, because detections are useful to a ground station through
     * the phone-as-edge link, but it cannot match anyone, so the screen says so.
     */
    NO_TARGET(canStart = true),

    /** The phone camera is the frame source but the device has no camera. A drone can still be used. */
    NO_CAMERA(canStart = false),
}

/**
 * Decides [SearchReadiness]. A missing camera comes first, because it is the
 * one state in which nothing can be searched.
 *
 * @param hasTarget Whether a target photo has been chosen.
 * @param hasCamera Whether the device has a back or front camera the preview can use (not only an external one).
 * @param frameSourceMode Where frames come from: the phone camera or a connected drone.
 */
fun searchReadiness(
    hasTarget: Boolean,
    hasCamera: Boolean,
    frameSourceMode: FrameSourceMode,
): SearchReadiness = when {
    frameSourceMode == FrameSourceMode.CAMERA && !hasCamera -> SearchReadiness.NO_CAMERA
    !hasTarget -> SearchReadiness.NO_TARGET
    else -> SearchReadiness.READY
}
