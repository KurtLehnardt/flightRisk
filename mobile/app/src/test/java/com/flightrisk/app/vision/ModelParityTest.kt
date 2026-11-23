package com.flightrisk.app.vision

import com.flightrisk.app.byo.ByoDetectorChecks
import com.flightrisk.app.byo.CheckScene
import com.flightrisk.contract.ReidEmbedding
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import kotlin.math.abs

/**
 * Runs the installed models through the app's own code (the same
 * [OnnxFloatModel], [DetectorEngine] and [EmbedderEngine] the phone runs, on
 * the desktop ONNX Runtime) and compares them with PyTorch's golden outputs.
 *
 * Opt-in: install the models into a scratch folder and pass it, together with
 * the golden fixtures, for example
 *
 *     python3 scripts/mobile_models/make_models.py install --targets android --android /tmp/assets
 *     ./gradlew :app:testDebugUnitTest -Pflightrisk.modelTestDir=/tmp/assets
 *
 * The tolerances are the ones `verify` gates the Android defaults on
 * (`golden.json`, `compare_with`).
 */
class ModelParityTest {

    private fun open(name: String): OnnxFloatModel = try {
        OnnxFloatModel.open(GoldenFixtures.requireModel(name).path)
    } catch (e: LinkageError) {
        assumeTrue("the desktop ONNX Runtime is not on the test classpath (${e.javaClass.simpleName})", false)
        throw e
    }

    private fun goldenLetterbox(): Letterbox = GoldenFixtures.frameSize.let { (w, h) -> YoloxContract.scale(w, h) }

    @Test
    fun `YOLOX-S on the golden input matches PyTorch's output`() {
        GoldenFixtures.requireDir()
        open(YoloxContract.ASSET).use { model ->
            assertArrayEquals(longArrayOf(1, 3, 640, 640), model.inputShape)
            assertEquals(YoloxContract.INPUT_NAME, model.inputName)
            val out = model.run(GoldenFixtures.floats("detector_input.f32"), YoloxContract.inputShape)
            assertArrayEquals(longArrayOf(1, 8400, 85), out.shape)
            val golden = GoldenFixtures.floats("detector_output.f32")
            var scoreDiff = 0f
            for (r in 0 until YoloxContract.NUM_ROWS) {
                val o = r * YoloxContract.ROW_LENGTH
                for (c in 0 until YoloxContract.NUM_CLASSES) {
                    val a = out.values[o + 4] * out.values[o + 5 + c]
                    val b = golden[o + 4] * golden[o + 5 + c]
                    scoreDiff = maxOf(scoreDiff, abs(a - b))
                }
            }
            println("YOLOX-S vs PyTorch on the golden input: largest score difference $scoreDiff")
            assertTrue("largest score difference $scoreDiff", scoreDiff <= 1e-3f)
            val dets = YoloxContract.decodeAllClasses(out.values, goldenLetterbox(), 0.25f)
            val expected = GoldenFixtures.goldenDetections()
            assertEquals(expected.map { it.classId }, dets.map { it.classId })
            for ((e, a) in expected.zip(dets)) assertTrue(Nms.iou(e, a) >= 0.999)
        }
    }

    @Test
    fun `the app's detector on the golden frame finds the golden people and cuts the golden crop`() {
        val (frame, w, h) = GoldenFixtures.png("frame.png")
        val engine = DetectorEngine(open(YoloxContract.ASSET), YoloxContract, 0.25f, DetectionSizeFilter.NONE)
        engine.use {
            val persons = it.detect(frame, w, h)
            val expected = GoldenFixtures.goldenDetections().filter { d -> d.classId == COCO_PERSON }
            assertEquals(expected.size, persons.size)
            for ((e, a) in expected.zip(persons)) {
                assertTrue("IoU ${Nms.iou(e, a.box)}", Nms.iou(e, a.box) >= 0.99)
                assertEquals(e.score, a.box.score, 0.01f)
            }
            println("app detector on the golden frame: IoU " + expected.zip(persons).map { (e, a) -> "%.5f".format(Nms.iou(e, a.box)) } +
                ", score difference " + expected.zip(persons).map { (e, a) -> "%.5f".format(kotlin.math.abs(e.score - a.box.score)) })
            val crop = persons.first().pixels
            val golden = GoldenFixtures.goldenCropPixels()
            for (i in 0 until 4) assertTrue("crop $crop vs $golden", abs(crop[i] - golden[i]) <= 1)
        }
    }

    @Test
    fun `the embedder matches PyTorch's embedding of the golden crop`() {
        GoldenFixtures.requireDir()
        val golden = GoldenFixtures.floats("embedding.f32")
        open(ClipContract.ASSET).use { model ->
            assertEquals(ClipContract.INPUT_NAME, model.inputName)
            // PyTorch's own input: only the model differs.
            val direct = ClipContract.l2Normalize(model.run(GoldenFixtures.floats("embedder_input.f32"), ClipContract.INPUT_SHAPE).values)
            val cosDirect = ClipContract.cosine(golden, direct)
            println("embedder vs PyTorch on the golden input: cosine $cosDirect")
            assertTrue("cosine $cosDirect", cosDirect >= 0.995)
        }
        // The app's whole path: crop rule on the frame, preprocessing, model, normalisation.
        val (frame, w, _) = GoldenFixtures.png("frame.png")
        val px = GoldenFixtures.goldenCropPixels()
        val crop = CropRule.cut(frame, w, px)
        EmbedderEngine(open(ClipContract.ASSET)).use { engine ->
            val e = engine.embed(crop, px[2] - px[0], px[3] - px[1])
            val cos = ClipContract.cosine(golden, e)
            println("app embedder path on the golden frame: cosine $cos")
            assertTrue("cosine $cos", cos >= 0.995)
            // It goes on the wire unchanged, under the ground's model id.
            assertEquals(ReidEmbedding.DIMENSION, ReidEmbedding.toWire(e).size)
        }
    }

    @Test
    fun `YOLOX-S finds people in the check scene an imported detector must pass`() {
        val engine = DetectorEngine(open(YoloxContract.ASSET), YoloxContract, ByoDetectorChecks.CHECK_SCORE, DetectionSizeFilter.NONE)
        engine.use {
            val persons = it.detect(CheckScene.pixels(), CheckScene.WIDTH, CheckScene.HEIGHT)
            println("YOLOX-S on the check scene: person scores ${persons.map { p -> "%.3f".format(p.box.score) }}")
            assertTrue("persons: ${persons.map { p -> p.box.score }}", persons.count { p -> p.box.score >= 0.5f } >= 2)
        }
    }

    @Test
    fun `the installed manifest lists both models`() {
        val manifest = File(GoldenFixtures.requireModel("mobile-models.json").path).readText()
        assertTrue(manifest.contains("\"${YoloxContract.ASSET}\"") && manifest.contains("\"${ClipContract.ASSET}\""))
        GoldenFixtures.requireModel("model-notices.md")
    }

    @Test
    fun `an imported detector passes the same checks as on the phone`() {
        // Opt-in: -Pflightrisk.byoTestModel=/path/to/detector.onnx. The file is only read.
        val path = System.getProperty("flightrisk.byoTestModel")
        assumeTrue("no detector to check (set -Pflightrisk.byoTestModel)", path != null && File(path).isFile)
        val model = try {
            OnnxFloatModel.open(path!!)
        } catch (e: LinkageError) {
            assumeTrue("the desktop ONNX Runtime is not on the test classpath", false)
            throw e
        }
        model.use {
            val (layout, score) = ByoDetectorChecks.checkModel(it)
            println("imported detector check: layout $layout, best person score $score")
            assertTrue(score >= ByoDetectorChecks.CHECK_SCORE)
        }
    }
}
