package com.flightrisk.app.vision

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assume.assumeTrue
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.util.zip.InflaterInputStream

/**
 * Access to the golden fixtures of `scripts/mobile_models/make_models.py
 * verify` (a drawn frame, no photograph) for the vision tests.
 *
 * - [numbers]: the numbers exported into the repository by
 *   `mobile/scripts/export_golden_numbers.py` (always present).
 * - [dir]: the full fixtures in `build/mobile_models/golden` (gitignored; the
 *   Gradle property `flightrisk.goldenDir` points elsewhere). Tests that need
 *   them are skipped when they are missing.
 * - [modelDir]: a folder of installed models (`-Pflightrisk.modelTestDir=...`,
 *   the folder `make_models.py install --android` wrote). Tests that run the
 *   models are skipped without it.
 */
object GoldenFixtures {
    val dir: File? = System.getProperty("flightrisk.goldenDir")?.let(::File)?.takeIf { File(it, "golden.json").isFile }

    val modelDir: File? = System.getProperty("flightrisk.modelTestDir")?.let(::File)?.takeIf { it.isDirectory }

    fun requireDir(): File {
        assumeTrue("golden fixtures not found (run make_models.py verify, or set -Pflightrisk.goldenDir)", dir != null)
        return dir!!
    }

    fun requireModel(name: String): File {
        assumeTrue("no installed models (set -Pflightrisk.modelTestDir)", modelDir != null)
        val f = File(modelDir, name)
        assumeTrue("$name is not in ${modelDir?.path}", f.isFile)
        return f
    }

    val numbers: JsonObject by lazy {
        val text = GoldenFixtures::class.java.getResource("/golden/golden-numbers.json")?.readText()
            ?: error("golden-numbers.json is missing from the test resources")
        Json.parseToJsonElement(text).jsonObject
    }

    /** A little-endian float32 file of the fixtures. */
    fun floats(name: String): FloatArray {
        val bytes = File(requireDir(), name).readBytes()
        val buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
        return FloatArray(buf.remaining()).also { buf.get(it) }
    }

    /** A PNG of the fixtures as (ARGB pixels, width, height). */
    fun png(name: String): Triple<IntArray, Int, Int> = decodePng(File(requireDir(), name).readBytes())

    /**
     * Decodes an 8-bit, non-interlaced RGB or RGBA PNG (what OpenCV and Pillow
     * write for the fixtures). The unit tests compile against android.jar,
     * which has no javax.imageio, so this small decoder stands in for it.
     */
    fun decodePng(bytes: ByteArray): Triple<IntArray, Int, Int> {
        val input = DataInputStream(bytes.inputStream())
        val signature = ByteArray(8).also { input.readFully(it) }
        require(signature.contentEquals(byteArrayOf(-119, 80, 78, 71, 13, 10, 26, 10))) { "not a PNG" }
        var width = 0
        var height = 0
        var channels = 0
        val idat = ByteArrayOutputStream()
        while (true) {
            val length = input.readInt()
            val type = String(ByteArray(4).also { input.readFully(it) }, Charsets.US_ASCII)
            val data = ByteArray(length).also { input.readFully(it) }
            input.readInt() // CRC
            when (type) {
                "IHDR" -> {
                    val d = DataInputStream(data.inputStream())
                    width = d.readInt()
                    height = d.readInt()
                    val depth = d.readUnsignedByte()
                    val colour = d.readUnsignedByte()
                    d.readUnsignedByte()
                    d.readUnsignedByte()
                    val interlace = d.readUnsignedByte()
                    require(depth == 8 && interlace == 0 && (colour == 2 || colour == 6)) { "unsupported PNG" }
                    channels = if (colour == 2) 3 else 4
                }
                "IDAT" -> idat.write(data)
                "IEND" -> break
            }
        }
        val raw = InflaterInputStream(idat.toByteArray().inputStream()).readBytes()
        val stride = width * channels
        val prev = IntArray(stride)
        val cur = IntArray(stride)
        val out = IntArray(width * height)
        for (y in 0 until height) {
            val base = y * (stride + 1)
            val filter = raw[base].toInt() and 0xFF
            for (i in 0 until stride) {
                val x = raw[base + 1 + i].toInt() and 0xFF
                val a = if (i >= channels) cur[i - channels] else 0
                val b = prev[i]
                val c = if (i >= channels) prev[i - channels] else 0
                cur[i] = (x + when (filter) {
                    0 -> 0
                    1 -> a
                    2 -> b
                    3 -> (a + b) / 2
                    4 -> {
                        val p = a + b - c
                        val pa = kotlin.math.abs(p - a)
                        val pb = kotlin.math.abs(p - b)
                        val pc = kotlin.math.abs(p - c)
                        if (pa <= pb && pa <= pc) a else if (pb <= pc) b else c
                    }
                    else -> error("bad PNG filter $filter")
                }) and 0xFF
            }
            for (x in 0 until width) {
                val o = x * channels
                out[y * width + x] = (0xFF shl 24) or (cur[o] shl 16) or (cur[o + 1] shl 8) or cur[o + 2]
            }
            cur.copyInto(prev)
        }
        return Triple(out, width, height)
    }

    /** The full `[8400, 85]` detector output rebuilt from the exported rows (every other row is zero). */
    fun detectorOutputFromNumbers(): FloatArray {
        val out = FloatArray(YoloxContract.NUM_ROWS * YoloxContract.ROW_LENGTH)
        for (row in numbers.getValue("rows").jsonArray) {
            val index = row.jsonObject.getValue("index").jsonPrimitive.content.toInt()
            val values = row.jsonObject.getValue("values").jsonArray
            for ((k, v) in values.withIndex()) out[index * YoloxContract.ROW_LENGTH + k] = v.jsonPrimitive.content.toFloat()
        }
        return out
    }

    /** The golden detections (all classes, score 0.25 or more), as the exported numbers record them. */
    fun goldenDetections(): List<FrameBox> = numbers.getValue("detections").jsonArray.map { d ->
        val o = d.jsonObject
        val box = o.getValue("box").jsonArray.map { it.jsonPrimitive.content.toDouble().toFloat() }
        FrameBox(box[0], box[1], box[2], box[3], o.getValue("score").jsonPrimitive.content.toDouble().toFloat(),
            o.getValue("class").jsonPrimitive.content.toInt())
    }

    fun goldenEmbedding(): FloatArray =
        numbers.getValue("embedding").jsonArray.map { it.jsonPrimitive.content.toFloat() }.toFloatArray()

    fun goldenCropPixels(): IntArray =
        numbers.getValue("crop_box").jsonObject.getValue("pixels").jsonArray.map { it.jsonPrimitive.content.toInt() }.toIntArray()

    val letterboxRatio: Float get() = numbers.getValue("letterbox_ratio").jsonPrimitive.content.toDouble().toFloat()

    val frameSize: Pair<Int, Int>
        get() = numbers.getValue("frame_size").jsonArray.let { it[0].jsonPrimitive.content.toInt() to it[1].jsonPrimitive.content.toInt() }
}
