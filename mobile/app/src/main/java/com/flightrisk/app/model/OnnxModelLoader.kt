package com.flightrisk.app.model

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.io.Closeable

/**
 * Shared utility for loading and running ONNX models on Android.
 *
 * Loads model bytes from the app's assets directory, creates an
 * [OrtSession], and provides a helper for running inference.
 * Call [close] when done to release native resources.
 */
class OnnxModelLoader(
    context: Context,
    assetFileName: String,
) : Closeable {

    val environment: OrtEnvironment = OrtEnvironment.getEnvironment()
    val session: OrtSession

    init {
        val modelBytes = context.assets.open(assetFileName).use { it.readBytes() }
        session = environment.createSession(modelBytes)
    }

    /**
     * Run inference with a single named input tensor.
     *
     * @param inputName The name of the input node (e.g. "images").
     * @param input The [OnnxTensor] to feed.
     * @return The [OrtSession.Result] containing output tensors.
     */
    fun runInference(inputName: String, input: OnnxTensor): OrtSession.Result {
        return session.run(mapOf(inputName to input))
    }

    /**
     * Run inference with the session's first input name inferred automatically.
     */
    fun runInference(input: OnnxTensor): OrtSession.Result {
        val inputName = session.inputNames.first()
        return runInference(inputName, input)
    }

    override fun close() {
        session.close()
    }
}
