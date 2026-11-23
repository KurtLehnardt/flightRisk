package com.flightrisk.app.byo

import com.flightrisk.app.model.OnnxModelScanner
import com.flightrisk.app.vision.COCO_PERSON
import com.flightrisk.app.vision.DetectionSizeFilter
import com.flightrisk.app.vision.DetectorEngine
import com.flightrisk.app.vision.FrameBox
import com.flightrisk.app.vision.OnnxFloatModel
import com.flightrisk.app.vision.UltralyticsContract
import java.io.File

/**
 * The checks a detector the user imports must pass before the app uses it
 * (Settings, "Bring your own detector"). They use no Android types, so they
 * are tested on the JVM; [ByoDetectorImporter] runs them on the phone.
 *
 * FlightRisk does not supply, bundle, host or license any such model. The
 * imported file stays in the app's private storage, excluded from backup, and
 * neither the file nor its contents are ever logged or sent anywhere.
 */
object ByoDetectorChecks {
    /** Largest file accepted: 100 MB (104,857,600 bytes). */
    const val MAX_BYTES: Long = 100L * 1024 * 1024

    /** The input every accepted model must declare. */
    val INPUT_SHAPE = longArrayOf(1, 3, 640, 640)

    /** Score at which a person on the check image counts as found. */
    const val CHECK_SCORE = 0.25f

    /** A failed check, with the sentence the user sees. */
    class Failure(val reason: String) : Exception(reason)

    /** Fails unless the display name ends in `.onnx`. */
    fun checkName(displayName: String?) {
        if (displayName == null || !displayName.trim().lowercase().endsWith(".onnx")) {
            throw Failure("Choose an .onnx file.")
        }
    }

    /** Fails when [bytes] (if known) is over [MAX_BYTES] or the file is empty. */
    fun checkSize(bytes: Long?) {
        if (bytes == null) return
        if (bytes <= 0L) throw Failure("The file is empty.")
        if (bytes > MAX_BYTES) throw Failure("The file is larger than 100 MB.")
    }

    /** Fails unless the model's input is exactly `[1, 3, 640, 640]` (a dynamic dimension is refused). */
    fun checkInput(shape: LongArray) {
        if (!shape.contentEquals(INPUT_SHAPE)) {
            throw Failure("The model's input must be [1, 3, 640, 640]; it is ${shape.contentToString()}.")
        }
    }

    /** The output layout, or a failure when the first output is neither `[1, 84, N]` nor `[1, 300, 6]`. */
    fun checkOutput(shape: LongArray): UltralyticsContract.Layout =
        UltralyticsContract.layoutOf(shape)
            ?: throw Failure(
                "The model's output must be [1, 84, N] (80 COCO classes) or [1, 300, 6]; it is ${shape.contentToString()}.",
            )

    /**
     * The class names an export records in its `names` metadata, for example
     * `{0: 'person', 1: 'bicycle'}`, or null when there are none.
     */
    fun parseNames(metadata: String?): Map<Int, String>? {
        if (metadata.isNullOrBlank()) return null
        val entry = Regex("""["']?(\d+)["']?\s*:\s*(?:'([^']*)'|"([^"]*)")""")
        val names = entry.findAll(metadata).associate { m ->
            m.groupValues[1].toInt() to (m.groupValues[2].ifEmpty { m.groupValues[3] })
        }
        return names.ifEmpty { null }
    }

    /** Fails when the model names its classes and class 0 is not "person". */
    fun checkClassNames(metadata: String?) {
        val names = parseNames(metadata) ?: return
        val first = names[COCO_PERSON]
        if (first == null || first.trim().lowercase() != "person") {
            throw Failure("The model's class 0 must be \"person\"; it is \"${first ?: "missing"}\".")
        }
    }

    /** The best person found on the check image, or a failure when there is none at [CHECK_SCORE] or above. */
    fun checkPersonFound(persons: List<FrameBox>): FrameBox =
        persons.filter { it.classId == COCO_PERSON && it.score >= CHECK_SCORE }.maxByOrNull { it.score }
            ?: throw Failure("The model found no person on the built-in check image, so it cannot be used.")

    /**
     * Fails when [file] is not a well-formed ONNX model, keeps tensor data in
     * other files (external data, which ONNX Runtime would read from wherever
     * the model points), or is too large a graph. Run it before ONNX Runtime
     * opens the file.
     */
    fun checkFileIsSafe(file: File) {
        try {
            OnnxModelScanner.scan(file)
        } catch (e: OnnxModelScanner.UnsafeModel) {
            throw Failure(e.message ?: "The model is not safe to open.")
        }
    }

    /**
     * Runs every check on an opened model: its input and output shapes, its
     * class names, and a person on [CheckScene]. Returns the output layout
     * and the best person score. Does not close [model].
     */
    fun checkModel(model: OnnxFloatModel): Pair<UltralyticsContract.Layout, Float> {
        checkInput(model.inputShape)
        val layout = checkOutput(model.outputShapes.firstOrNull() ?: LongArray(0))
        checkClassNames(model.session.metadata.customMetadata["names"])
        val engine = DetectorEngine(model, UltralyticsContract(layout), CHECK_SCORE, DetectionSizeFilter.NONE)
        val found = checkPersonFound(engine.detect(CheckScene.pixels(), CheckScene.WIDTH, CheckScene.HEIGHT).map { it.box })
        return layout to found.score
    }

    /** Lower-case hexadecimal form of a digest. */
    fun hex(digest: ByteArray): String = digest.joinToString("") { "%02x".format(it.toInt() and 0xFF) }

    /** The SHA-256 grouped in blocks of eight, for reading on a small screen. */
    fun groupedHex(sha256: String): String = sha256.chunked(8).joinToString(" ")
}
