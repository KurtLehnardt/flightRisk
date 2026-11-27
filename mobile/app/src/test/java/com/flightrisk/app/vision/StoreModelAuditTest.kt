package com.flightrisk.app.vision

import com.flightrisk.app.config.MatchingAlgorithmConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Source audit: store builds use only the models `make_models.py install`
 * provides (YOLOX-S and the open_clip ViT-B-16 embedder). No face model and no
 * model with non-commercial or AGPL weights is loaded.
 */
class StoreModelAuditTest {

    private val app: File = listOf(File("."), File("app"), File("mobile/app"))
        .first { File(it, "src/main/java/com/flightrisk/app").isDirectory }

    /**
     * The main sources. `VisionConfig.detectorModel` mirrors the ground's Python
     * default (`yolo11n.pt`, the ground's own detector) and is not used by the
     * app, so that one line is left out.
     */
    private fun sources(): Map<String, String> =
        File(app, "src/main/java").walkTopDown().filter { it.isFile && it.extension == "kt" }
            .associate { f ->
                f.relativeTo(File(app, "src/main/java")).invariantSeparatorsPath to
                    f.readText().replace("val detectorModel: String = \"yolo11n.pt\",", "")
            }

    @Test
    fun `no code loads a face, InsightFace, OSNet, MobileCLIP or old YOLO model`() {
        val forbidden = Regex(
            """FaceRecognizer|InsightFace|insightface|OSNet|osnet|scrfd|arcface|ArcFace|mobileclip|MobileCLIP|""" +
                """yolo11n|yolov8|clip_visual|org\.tensorflow\.lite|buffalo_|antelope""",
        )
        val offenders = sources().filter { (_, text) -> forbidden.containsMatchIn(text) }
            .mapValues { (_, text) -> forbidden.findAll(text).map { it.value }.toSet() }
        assertTrue("forbidden model names in: $offenders", offenders.isEmpty())
    }

    @Test
    fun `the app loads only the installed store models by name`() {
        val assetNames = Regex(""""([A-Za-z0-9_\-]+\.(onnx|tflite|pt|mlmodel))"""")
        val names = sources().values.flatMap { text -> assetNames.findAll(text).map { it.groupValues[1] }.toList() }.toSet()
        // midas_small.onnx is the obstacle guard's, outside this pipeline (docs/mobile-models.md, follow-ups).
        assertEquals(setOf(YoloxContract.ASSET, ClipContract.ASSET, "midas_small.onnx", "detector.onnx"), names)
    }

    @Test
    fun `the build has no LiteRT or TensorFlow Lite runtime`() {
        val gradle = File(app, "build.gradle.kts").readText()
        val deps = gradle.lines().filter { it.trim().startsWith("implementation(") }
        assertTrue(deps.none { "litert" in it || "tensorflow" in it })
        assertTrue(gradle.contains("noCompress += \"onnx\""))
    }

    @Test
    fun `matching is by appearance only`() {
        val keys = MatchingAlgorithmConfig.SIGNALS.map { it.key }
        assertTrue(keys.none { "face" in it || "osnet" in it })
        assertTrue("reid" in keys)
    }

    /** The text of [source] from [from] up to (not including) [to]; both must be present. */
    private fun between(source: String, from: String, to: String): String {
        val a = source.indexOf(from)
        assertTrue("missing: $from", a >= 0)
        val b = source.indexOf(to, a + from.length)
        assertTrue("missing after $from: $to", b > a)
        return source.substring(a, b)
    }

    @Test
    fun `switching the demo on and Stop both run the real stop path`() {
        val activity = File(app, "src/main/java/com/flightrisk/app/MainActivity.kt").readText()
        val stopPath = between(activity, "private fun stopRealSearch()", "private fun handleDismissAlert()")
        for (step in listOf(
            "pipelineSetupJob?.cancel()", "searchPipeline?.stop(",
        )) assertTrue("stop path lacks $step", stopPath.contains(step))
        assertTrue(stopPath.indexOf("searchPipeline?.stop(") < stopPath.indexOf("droneManager?.stopSearchPattern()"))

        // applyDemo(true) stops a running search before the demo takes over.
        val applyDemo = between(activity, "private fun applyDemo(", "private fun startDemoSearch(")
        val stop = applyDemo.indexOf("if (on && realSearchRunning()) stopRealSearch()")
        assertTrue("applyDemo must run the stop path", stop >= 0)
        assertTrue(stop < applyDemo.indexOf("demoActive = on"))

        // Stop runs the stop path before the demo branch, so the demo cannot skip it.
        val handleStop = between(activity, "private fun handleStopSearch()", "private fun realSearchRunning()")
        val run = handleStop.indexOf("if (!demoActive || realSearchRunning()) stopRealSearch()")
        assertTrue("handleStopSearch must run the stop path", run >= 0)
        assertTrue(run < handleStop.indexOf("if (demoActive) {"))

        val running = between(activity, "private fun realSearchRunning()", "private fun stopRealSearch()")
        for (part in listOf("pipelineSetupJob != null", "searchPipeline != null")) {
            assertTrue("realSearchRunning lacks $part", running.contains(part))
        }
    }
}
