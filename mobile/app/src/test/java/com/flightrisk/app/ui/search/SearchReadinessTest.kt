package com.flightrisk.app.ui.search

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * What the Search screen says before a search, and whether Start may begin one.
 * A search needs a connected drone: the phone's own camera is never a frame
 * source. After "Set up a search later" there is also no target photo.
 */
class SearchReadinessTest {

    private val app: File = listOf(File("."), File("app"), File("mobile/app"))
        .first { File(it, "src/main/java/com/flightrisk/app").isDirectory }

    private fun main(path: String) = File(app, "src/main/java/com/flightrisk/app/$path").readText()

    @Test
    fun `a drone and a target make a search ready`() {
        assertEquals(SearchReadiness.READY, searchReadiness(hasTarget = true, droneConnected = true))
    }

    @Test
    fun `no target photo is shown but does not stop a search`() {
        assertEquals(SearchReadiness.NO_TARGET, searchReadiness(hasTarget = false, droneConnected = true))
        assertTrue(SearchReadiness.NO_TARGET.canStart)
    }

    @Test
    fun `without a drone Start does not begin a search, with or without a target`() {
        assertEquals(SearchReadiness.NO_DRONE, searchReadiness(hasTarget = true, droneConnected = false))
        assertEquals(SearchReadiness.NO_DRONE, searchReadiness(hasTarget = false, droneConnected = false))
        assertFalse(SearchReadiness.NO_DRONE.canStart)
    }

    @Test
    fun `the screen state starts with no drone, idle, and Start refused`() {
        val state = SearchScreenState()
        assertFalse(state.droneConnected)
        assertFalse(state.isSearching)
        assertEquals(SearchReadiness.NO_DRONE, state.readiness)
        assertFalse(state.readiness.canStart)
        assertEquals(SearchReadiness.NO_TARGET, SearchScreenState(droneConnected = true, hasTarget = false).readiness)
        assertEquals(SearchReadiness.READY, SearchScreenState(droneConnected = true).readiness)
    }

    @Test
    fun `the demo search needs no drone`() {
        assertEquals(SearchReadiness.READY, SearchScreenState(demoMode = true).readiness)
    }

    @Test
    fun `the no drone message is short and says what to do`() {
        val strings = File(app, "src/main/res/values/strings.xml").readText()
        assertTrue(strings.contains("<string name=\"search_no_drone_title\">Connect a drone first.</string>"))
    }

    @Test
    fun `handleStartSearch refuses before it marks the search as running`() {
        val activity = main("MainActivity.kt")
        val start = activity.indexOf("private fun handleStartSearch()")
        val stop = activity.indexOf("private fun handleStopSearch()")
        val body = activity.substring(start, stop)
        val guard = body.indexOf("if (!readiness.canStart)")
        val running = body.indexOf("isSearching = true")
        assertTrue("the guard must exist", guard >= 0)
        assertTrue("the guard must come before the search is marked running", guard < running)
        assertTrue(body.substring(guard, running).contains("return"))
        assertTrue(body.contains("droneConnected = droneStreaming"))
    }

    @Test
    fun `no code opens the phone camera`() {
        val banned = listOf(
            "androidx.camera", "CameraXFrameSource", "FrameSourceMode", "ProcessCameraProvider",
            "Manifest.permission.CAMERA", "LiveCameraPreview", "NO_CAMERA",
        )
        File(app, "src/main/java").walkTopDown().filter { it.isFile && it.extension == "kt" }.forEach { f ->
            val text = f.readText()
            for (word in banned) assertFalse("${f.name} still mentions $word", text.contains(word))
        }
        // "Take Photo" for the target photo hands over to the camera app and does not need a camera permission.
        assertTrue(main("ui/target/TargetPickerScreen.kt").contains("ActivityResultContracts.TakePicture()"))
    }
}
