package com.flightrisk.app.demo

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The demo's pictures are drawn, not photographed, and the demo code has no way onto the network.
 */
class DemoArtAndSourceTest {

    @Test
    fun `the placeholder target is a drawing in a handful of solid colours`() {
        val px = DemoArt.targetPixels()
        assertEquals(DemoArt.TARGET_WIDTH * DemoArt.TARGET_HEIGHT, px.size)
        val colours = px.toSet()
        // A photo has thousands of colours; the placeholder uses only the declared palette.
        assertTrue("colours: ${colours.size}", colours.size <= 8)
        assertTrue(DemoArt.PALETTE.containsAll(colours))
        assertTrue(colours.contains(DemoArt.JACKET) && colours.contains(DemoArt.STRIPE))
        // It is the same drawing every time.
        assertArrayEquals(px, DemoArt.targetPixels())
    }

    @Test
    fun `the demo scene is a drawing with the figure inside its detection box`() {
        val px = DemoArt.scenePixels()
        val w = DemoArt.SCENE_WIDTH
        assertEquals(w * DemoArt.SCENE_HEIGHT, px.size)
        assertTrue(DemoArt.PALETTE.containsAll(px.toSet()))
        val (x1, y1, x2, y2) = DemoArt.SCENE_FIGURE_BOX.toList()
        assertTrue(x1 in 0 until x2 && x2 <= w && y1 in 0 until y2 && y2 <= DemoArt.SCENE_HEIGHT)
        val inside = (y1 until y2).flatMap { y -> (x1 until x2).map { x -> px[y * w + x] } }.toSet()
        assertTrue(inside.contains(DemoArt.JACKET))
    }

    private val demoSources: Map<String, String> by lazy {
        val dir = listOf(File("src/main/java"), File("app/src/main/java"), File("mobile/app/src/main/java"))
            .map { File(it, "com/flightrisk/app/demo") }.first { it.isDirectory }
        // Code only: comments may name what the demo does not use.
        dir.listFiles { f -> f.extension == "kt" }!!.associate { f ->
            f.name to f.readLines().filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("/") }
                .joinToString("\n") { it.substringBefore("//") }
        }
    }

    @Test
    fun `the demo package has no network code`() {
        val network = Regex(
            "java\\.net|javax\\.net|okhttp3|\\bSocket\\b|SocketFactory|URL\\(|HttpURLConnection|RawWebSocket|" +
                "WebSocketSocketIoPort|DashboardRestClient|EdgePublisher|GroundLinkController|InetAddress",
        )
        assertTrue(demoSources.isNotEmpty())
        val offenders = demoSources.filter { network.containsMatchIn(it.value) }.keys
        assertTrue("network code in the demo package: $offenders", offenders.isEmpty())
    }

    @Test
    fun `the demo ships no image file`() {
        val assets = listOf(File("src/main/assets"), File("app/src/main/assets"), File("mobile/app/src/main/assets")).first { it.isDirectory }
        val images = assets.walkTopDown().filter { it.isFile && it.extension.lowercase() in setOf("jpg", "jpeg", "png", "webp", "heic") }.toList()
        assertTrue("image files in the app assets: $images", images.isEmpty())
    }
}
