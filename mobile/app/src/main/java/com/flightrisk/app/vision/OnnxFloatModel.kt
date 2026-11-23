package com.flightrisk.app.vision

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import java.io.Closeable
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * An ONNX Runtime session with one float32 input and a float32 first output.
 * It uses only the ONNX Runtime Java API (no Android types), so the same code
 * runs on the phone and, with the desktop ONNX Runtime, in the JVM parity
 * tests. [run] is synchronized: the input buffer is reused between calls.
 */
class OnnxFloatModel(
    private val environment: OrtEnvironment,
    val session: OrtSession,
) : Closeable {

    /** The output of one run: the values and the shape the model reported. */
    class Output(val values: FloatArray, val shape: LongArray)

    val inputName: String = session.inputNames.first()

    /** The declared shape of the input (a dynamic dimension is -1). */
    val inputShape: LongArray get() = tensorShape(session.inputInfo.getValue(inputName).info)

    /** The declared shapes of the outputs, in order. */
    val outputShapes: List<LongArray>
        get() = session.outputInfo.values.map { tensorShape(it.info) }

    private var buffer: FloatBuffer? = null

    @Synchronized
    fun run(input: FloatArray, shape: LongArray): Output {
        val count = shape.fold(1L) { a, b -> a * b }.toInt()
        require(input.size >= count) { "the input has ${input.size} values, the shape needs $count" }
        val buf = buffer?.takeIf { it.capacity() >= count }
            ?: ByteBuffer.allocateDirect(count * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().also { buffer = it }
        buf.clear()
        buf.put(input, 0, count)
        buf.flip()
        OnnxTensor.createTensor(environment, buf, shape).use { tensor ->
            session.run(mapOf(inputName to tensor)).use { result ->
                val out = result.get(0) as? OnnxTensor ?: error("the model's first output is not a tensor")
                val values = out.floatBuffer
                val array = FloatArray(values.remaining())
                values.get(array)
                return Output(array, out.info.shape)
            }
        }
    }

    override fun close() {
        session.close()
    }

    companion object {
        /** The shape of a tensor input or output, or an empty array for anything else. */
        fun tensorShape(info: Any?): LongArray = (info as? TensorInfo)?.shape ?: LongArray(0)

        /** Opens [path] with the default options. */
        fun open(path: String): OnnxFloatModel {
            val env = OrtEnvironment.getEnvironment()
            return OnnxFloatModel(env, env.createSession(path, OrtSession.SessionOptions()))
        }

        /** Opens a model held in a direct (for example memory-mapped) buffer. */
        fun open(buffer: ByteBuffer): OnnxFloatModel {
            val env = OrtEnvironment.getEnvironment()
            return OnnxFloatModel(env, env.createSession(buffer, OrtSession.SessionOptions()))
        }
    }
}

/** One person the detector kept: the model's box in frame pixels and the integer crop box. */
class PersonBox(val box: FrameBox, val pixels: IntArray)

/**
 * The person detector without Android types: contract preprocessing, the
 * model, the contract's decoding, the crop rule and the size filter.
 */
class DetectorEngine(
    private val model: OnnxFloatModel,
    val contract: DetectorContract,
    val scoreThreshold: Float,
    val sizeFilter: DetectionSizeFilter = DetectionSizeFilter.AERIAL,
) : Closeable {
    private val input = FloatArray(3 * contract.inputSize * contract.inputSize)

    /** The kept persons in a [width] x [height] frame of ARGB pixels, highest score first. */
    @Synchronized
    fun detect(argb: IntArray, width: Int, height: Int): List<PersonBox> {
        val lb = contract.preprocess(argb, width, height, input)
        val out = model.run(input, contract.inputShape)
        return contract.decode(out.values, out.shape, lb, scoreThreshold).mapNotNull { box ->
            val px = CropRule.pixels(box, width, height) ?: return@mapNotNull null
            if (sizeFilter.keeps(px, width, height)) PersonBox(box, px) else null
        }
    }

    override fun close() = model.close()
}

/** The appearance embedder without Android types: contract preprocessing and the model. */
class EmbedderEngine(private val model: OnnxFloatModel) : Closeable {
    private val input = FloatArray(3 * ClipContract.INPUT_SIZE * ClipContract.INPUT_SIZE)

    /** The L2-normalised 512-value embedding of a [width] x [height] crop of ARGB pixels. */
    @Synchronized
    fun embed(argb: IntArray, width: Int, height: Int): FloatArray {
        ClipContract.preprocess(argb, width, height, input)
        val out = model.run(input, ClipContract.INPUT_SHAPE)
        check(out.values.size == ClipContract.EMBEDDING_DIM) { "the embedder returned ${out.values.size} values" }
        return ClipContract.l2Normalize(out.values)
    }

    override fun close() = model.close()
}
