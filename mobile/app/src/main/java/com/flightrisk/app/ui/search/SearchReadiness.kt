package com.flightrisk.app.ui.search

/**
 * Whether a search can start, and what the Search screen should say before it
 * does. Only [NO_DRONE] stops a search from starting: the phone's own camera is
 * never a frame source, so every search needs a connected drone.
 */
enum class SearchReadiness(
    /** Whether Start may begin a search in this state. */
    val canStart: Boolean,
) {
    /** A drone is connected and a target photo is set. */
    READY(canStart = true),

    /**
     * No target photo: onboarding was finished with "Set up a search later" or
     * the app was restarted (the photo is kept in memory only). A search can
     * still start, because detections are useful to a ground station through
     * the phone-as-edge link, but it cannot match anyone, so the screen says so.
     */
    NO_TARGET(canStart = true),

    /** No drone is connected, so there are no frames to search. Start does nothing until one connects. */
    NO_DRONE(canStart = false),
}

/**
 * Decides [SearchReadiness]. A missing drone comes first, because it is the
 * one state in which nothing can be searched.
 *
 * @param hasTarget Whether a target photo has been chosen.
 * @param droneConnected Whether a drone is connected and streaming video.
 */
fun searchReadiness(
    hasTarget: Boolean,
    droneConnected: Boolean,
): SearchReadiness = when {
    !droneConnected -> SearchReadiness.NO_DRONE
    !hasTarget -> SearchReadiness.NO_TARGET
    else -> SearchReadiness.READY
}
