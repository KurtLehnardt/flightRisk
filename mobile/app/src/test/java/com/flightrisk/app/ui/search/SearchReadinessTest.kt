package com.flightrisk.app.ui.search

import com.flightrisk.app.drone.FrameSourceMode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What the Search screen says before a search, and whether Start may begin one:
 * after "Set up a search later" there is no target photo, and some devices have
 * no camera.
 */
class SearchReadinessTest {

    @Test
    fun `a target and a camera make a search ready`() {
        assertEquals(SearchReadiness.READY, searchReadiness(hasTarget = true, hasCamera = true, FrameSourceMode.CAMERA))
        assertEquals(SearchReadiness.READY, searchReadiness(hasTarget = true, hasCamera = true, FrameSourceMode.DRONE))
    }

    @Test
    fun `no target photo is shown but does not stop a search`() {
        for (mode in FrameSourceMode.values()) {
            assertEquals(SearchReadiness.NO_TARGET, searchReadiness(hasTarget = false, hasCamera = true, mode))
        }
        assertTrue(SearchReadiness.NO_TARGET.canStart)
    }

    @Test
    fun `a device without a camera can search only with a drone`() {
        assertEquals(SearchReadiness.NO_CAMERA, searchReadiness(hasTarget = true, hasCamera = false, FrameSourceMode.CAMERA))
        assertEquals(SearchReadiness.NO_CAMERA, searchReadiness(hasTarget = false, hasCamera = false, FrameSourceMode.CAMERA))
        assertFalse(SearchReadiness.NO_CAMERA.canStart)
        assertEquals(SearchReadiness.READY, searchReadiness(hasTarget = true, hasCamera = false, FrameSourceMode.DRONE))
        assertEquals(SearchReadiness.NO_TARGET, searchReadiness(hasTarget = false, hasCamera = false, FrameSourceMode.DRONE))
    }

    @Test
    fun `the screen state defaults to ready and follows its fields`() {
        assertEquals(SearchReadiness.READY, SearchScreenState().readiness)
        assertEquals(SearchReadiness.NO_TARGET, SearchScreenState(hasTarget = false).readiness)
        assertEquals(SearchReadiness.NO_CAMERA, SearchScreenState(hasCamera = false).readiness)
        assertEquals(
            SearchReadiness.READY,
            SearchScreenState(hasCamera = false, frameSourceMode = FrameSourceMode.DRONE).readiness,
        )
    }
}
