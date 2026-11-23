package com.flightrisk.app.vision

import com.flightrisk.app.camera.FrameRotation
import com.flightrisk.app.config.MatchingAlgorithmConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Source audit: store builds use only the models `make_models.py install`
 * provides (YOLOX-S and the open_clip ViT-B-16 embedder). No face model and no
 * model with non-commercial or AGPL weights is loaded, and the phone camera
 * feeds the detector.
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

    @Test
    fun `the camera's frame analysis is bound with the preview and feeds the pipeline`() {
        val screen = File(app, "src/main/java/com/flightrisk/app/ui/search/SearchScreen.kt").readText()
        assertTrue(screen.contains("provider.bindToLifecycle(lifecycleOwner, cameraSelector, p, a)"))
        assertTrue(screen.contains("frameSource?.buildImageAnalysis("))
        val source = File(app, "src/main/java/com/flightrisk/app/camera/CameraXFrameSource.kt").readText()
        assertTrue(source.contains("OUTPUT_IMAGE_FORMAT_RGBA_8888"))
        assertTrue(source.contains("image.toBitmap()"))
        assertTrue(source.contains("image.imageInfo.rotationDegrees"))
        assertTrue(!source.contains("NV21") && !source.contains("YuvImage"))
        val activity = File(app, "src/main/java/com/flightrisk/app/MainActivity.kt").readText()
        assertTrue(activity.contains("cameraFrameSource.start()"))
        assertTrue(activity.contains("cameraFrameSource.takeNewFrame()"))
        assertTrue(activity.contains("cameraFrameSource.stop()"))
        assertTrue(activity.contains("cameraFrameSource = cameraFrameSource"))
    }

    @Test
    fun `rotations are whole quarter turns`() {
        assertEquals(0, FrameRotation.normalize(0))
        assertEquals(90, FrameRotation.normalize(90))
        assertEquals(270, FrameRotation.normalize(-90))
        assertEquals(180, FrameRotation.normalize(540))
        assertTrue(runCatching { FrameRotation.normalize(45) }.isFailure)
    }
}
