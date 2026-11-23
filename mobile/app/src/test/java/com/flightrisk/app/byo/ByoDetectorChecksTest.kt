package com.flightrisk.app.byo

import com.flightrisk.app.vision.FrameBox
import com.flightrisk.app.vision.UltralyticsContract.Layout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/** The checks an imported detector must pass, and the words around them. */
class ByoDetectorChecksTest {

    private fun fails(block: () -> Unit): String {
        val e = runCatching(block).exceptionOrNull()
        assertTrue("expected a failure, got ${e?.javaClass?.simpleName}", e is ByoDetectorChecks.Failure)
        return e!!.message!!
    }

    @Test
    fun `only onnx files are accepted`() {
        ByoDetectorChecks.checkName("detector.onnx")
        ByoDetectorChecks.checkName("My Detector.ONNX ")
        fails { ByoDetectorChecks.checkName("detector.pt") }
        fails { ByoDetectorChecks.checkName("detector.onnx.zip") }
        fails { ByoDetectorChecks.checkName(null) }
    }

    @Test
    fun `the size cap is 100 MB`() {
        ByoDetectorChecks.checkSize(null)
        ByoDetectorChecks.checkSize(1)
        ByoDetectorChecks.checkSize(104_857_600)
        assertTrue(fails { ByoDetectorChecks.checkSize(104_857_601) }.contains("100 MB"))
        fails { ByoDetectorChecks.checkSize(0) }
    }

    @Test
    fun `the input must be exactly 1 x 3 x 640 x 640`() {
        ByoDetectorChecks.checkInput(longArrayOf(1, 3, 640, 640))
        fails { ByoDetectorChecks.checkInput(longArrayOf(1, 3, 320, 320)) }
        fails { ByoDetectorChecks.checkInput(longArrayOf(-1, 3, 640, 640)) }
        fails { ByoDetectorChecks.checkInput(longArrayOf(1, 640, 640, 3)) }
    }

    @Test
    fun `the output must be 1 x 84 x N or 1 x 300 x 6`() {
        assertEquals(Layout.ANCHORS, ByoDetectorChecks.checkOutput(longArrayOf(1, 84, 8400)))
        assertEquals(Layout.END_TO_END, ByoDetectorChecks.checkOutput(longArrayOf(1, 300, 6)))
        // YOLOX's own layout, a one-class model, and a segmentation head are refused.
        fails { ByoDetectorChecks.checkOutput(longArrayOf(1, 8400, 85)) }
        fails { ByoDetectorChecks.checkOutput(longArrayOf(1, 5, 8400)) }
        fails { ByoDetectorChecks.checkOutput(longArrayOf(1, 116, 8400)) }
        fails { ByoDetectorChecks.checkOutput(LongArray(0)) }
    }

    @Test
    fun `class names are read from the export's metadata and class 0 must be person`() {
        val coco = "{0: 'person', 1: 'bicycle', 2: 'car'}"
        assertEquals(mapOf(0 to "person", 1 to "bicycle", 2 to "car"), ByoDetectorChecks.parseNames(coco))
        assertEquals("person", ByoDetectorChecks.parseNames("{\"0\": \"person\"}")?.get(0))
        ByoDetectorChecks.checkClassNames(coco)
        // No names: nothing to check here; the check scene decides.
        assertNull(ByoDetectorChecks.parseNames(null))
        ByoDetectorChecks.checkClassNames(null)
        ByoDetectorChecks.checkClassNames("")
        assertTrue(fails { ByoDetectorChecks.checkClassNames("{0: 'hiker', 1: 'person'}") }.contains("hiker"))
        fails { ByoDetectorChecks.checkClassNames("{1: 'person'}") }
    }

    @Test
    fun `a person must be found on the check scene at 0_25 or more`() {
        val weak = FrameBox(0f, 0f, 10f, 20f, 0.2f, 0)
        val strong = FrameBox(0f, 0f, 10f, 20f, 0.6f, 0)
        val car = FrameBox(0f, 0f, 10f, 20f, 0.9f, 2)
        assertEquals(strong, ByoDetectorChecks.checkPersonFound(listOf(weak, strong, car)))
        fails { ByoDetectorChecks.checkPersonFound(listOf(weak, car)) }
        fails { ByoDetectorChecks.checkPersonFound(emptyList()) }
    }

    @Test
    fun `the SHA-256 is shown in full, grouped`() {
        val sha = ByoDetectorChecks.hex(MessageDigest.getInstance("SHA-256").digest("abc".toByteArray()))
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", sha)
        assertEquals("ba7816bf 8f01cfea 414140de 5dae2223 b00361a3 96177a9c b410ff61 f20015ad", ByoDetectorChecks.groupedHex(sha))
        assertTrue(ByoDetectorText.imported(12_345_678, "anchors", sha).contains(ByoDetectorChecks.groupedHex(sha)))
    }

    @Test
    fun `the check scene is the same drawing every time`() {
        val px = CheckScene.pixels()
        assertEquals(CheckScene.WIDTH * CheckScene.HEIGHT, px.size)
        val rgb = ByteArray(px.size * 3)
        for ((i, p) in px.withIndex()) {
            rgb[3 * i] = (p shr 16).toByte()
            rgb[3 * i + 1] = (p shr 8).toByte()
            rgb[3 * i + 2] = p.toByte()
        }
        // The digest of the RGB bytes of the drawing the detectors were tried on.
        assertEquals(
            "a772b1f57b4cb05654eeae57c95e26ebab452673cd75fca9e7e2aba61c49e098",
            ByoDetectorChecks.hex(MessageDigest.getInstance("SHA-256").digest(rgb)),
        )
    }

    @Test
    fun `the gate explains both licences, links to them and asks for the exact attestation`() {
        val body = ByoDetectorText.GATE_BODY
        assertTrue(body.contains("AGPL-3.0") && body.contains("Ultralytics Enterprise License"))
        assertTrue(body.contains("does not include, supply, host or license any YOLO model"))
        assertTrue(body.contains("not backed up") && body.contains("deletes it"))
        assertEquals("https://www.ultralytics.com/license", ByoDetectorText.LICENSE_URL)
        assertEquals(
            "I have the right to use this model. FlightRisk does not supply or license YOLO.",
            ByoDetectorText.ATTESTATION,
        )
    }

    private val main: File = listOf(File("src/main/java"), File("app/src/main/java"), File("mobile/app/src/main/java"))
        .first { File(it, "com/flightrisk/app").isDirectory }

    private fun read(path: String) = File(main, path).readText()

    @Test
    fun `nothing offers, pre-fills or logs a model`() {
        val files = listOf(
            "com/flightrisk/app/byo/ByoDetectorChecks.kt",
            "com/flightrisk/app/byo/ByoDetectorImporter.kt",
            "com/flightrisk/app/byo/ByoDetectorStore.kt",
            "com/flightrisk/app/byo/ByoDetectorText.kt",
            "com/flightrisk/app/byo/CheckScene.kt",
            "com/flightrisk/app/ui/settings/ByoDetectorSection.kt",
        )
        for (f in files) {
            val text = read(f)
            // The only address anywhere in the feature is the licensing page.
            val urls = Regex("""https?://[^\s"')]+""").findAll(text).map { it.value }.toSet()
            assertTrue("$f names $urls", urls.all { it == ByoDetectorText.LICENSE_URL })
            assertTrue("$f logs", !Regex("""\bLog\.|println\(""").containsMatchIn(text))
        }
        // The picker opens with nothing pre-filled: no initial address.
        val section = read("com/flightrisk/app/ui/settings/ByoDetectorSection.kt")
        assertTrue(section.contains("ActivityResultContracts.OpenDocument()"))
        assertTrue(!section.contains("EXTRA_INITIAL_URI"))
    }

    @Test
    fun `the imported model stays in app-private storage that backup excludes`() {
        val store = read("com/flightrisk/app/byo/ByoDetectorStore.kt")
        assertTrue(store.contains("File(appContext.filesDir, FOLDER)"))
        assertTrue(store.contains("const val FOLDER = \"byo_detector\""))
        assertTrue(store.contains("const val PREFS_NAME = \"flightrisk_byo_detector\""))
    }
}
