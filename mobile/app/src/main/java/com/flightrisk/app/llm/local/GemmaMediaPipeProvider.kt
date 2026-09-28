package com.flightrisk.app.llm.local

import android.content.Context
import android.util.Log
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * [LocalLlmProvider] backed by Google's MediaPipe LLM Inference API
 * running Gemma 2 2B (int4 quantized).
 *
 * This is the first concrete provider — swap it out for llama.cpp,
 * ONNX GenAI, or any other runtime by implementing [LocalLlmProvider].
 *
 * The model file (~1.3 GB) is downloaded on demand to the app's
 * internal files directory and loaded via MediaPipe at inference time.
 */
class GemmaMediaPipeProvider(
    private val context: Context,
) : LocalLlmProvider {

    companion object {
        private const val TAG = "GemmaMediaPipe"
        private const val MODEL_FILENAME = "gemma2-2b-it-gpu-int4.bin"
        private const val DOWNLOAD_URL =
            "https://storage.googleapis.com/mediapipe-models/llm_inference/gemma2_2b_it_gpu_int4/float32/latest/model.bin"
        private const val BUFFER_SIZE = 8192
    }

    override val modelInfo = LocalModelInfo(
        id = "gemma-2-2b-it-int4",
        displayName = "Gemma 2 2B (int4)",
        sizeBytes = 1_400_000_000L,
        minRamMb = 4096,
        quantization = "int4",
        description = "Google Gemma 2 2B instruction-tuned, 4-bit quantized for on-device inference",
    )

    private val _state = MutableStateFlow<ModelState>(ModelState.Idle)
    override val state: StateFlow<ModelState> = _state.asStateFlow()

    override val isReady: Boolean
        get() = _state.value is ModelState.Ready

    private var inference: LlmInference? = null
    @Volatile
    private var downloadCancelled = false

    private val modelDir: File
        get() = File(context.filesDir, "models").also { it.mkdirs() }

    private val modelFile: File
        get() = File(modelDir, MODEL_FILENAME)

    init {
        if (modelFile.exists() && modelFile.length() > 0) {
            _state.value = ModelState.Downloaded
        }
    }

    override suspend fun download(onProgress: (Float) -> Unit) {
        val current = _state.value
        if (current !is ModelState.Idle && current !is ModelState.Error) return

        downloadCancelled = false
        _state.value = ModelState.Downloading(0f)

        withContext(Dispatchers.IO) {
            var connection: HttpURLConnection? = null
            try {
                connection = URL(DOWNLOAD_URL).openConnection() as HttpURLConnection
                connection.connectTimeout = 30_000
                connection.readTimeout = 30_000
                connection.connect()

                val totalBytes = connection.contentLengthLong
                val tempFile = File(modelDir, "$MODEL_FILENAME.tmp")

                connection.inputStream.use { input ->
                    tempFile.outputStream().use { output ->
                        val buffer = ByteArray(BUFFER_SIZE)
                        var bytesRead: Long = 0

                        while (isActive && !downloadCancelled) {
                            val read = input.read(buffer)
                            if (read == -1) break
                            output.write(buffer, 0, read)
                            bytesRead += read

                            if (totalBytes > 0) {
                                val progress = bytesRead.toFloat() / totalBytes
                                _state.value = ModelState.Downloading(progress)
                                onProgress(progress)
                            }
                        }
                    }
                }

                if (downloadCancelled) {
                    tempFile.delete()
                    _state.value = ModelState.Idle
                    return@withContext
                }

                tempFile.renameTo(modelFile)
                _state.value = ModelState.Downloaded
                Log.i(TAG, "Model downloaded: ${modelFile.length()} bytes")
            } catch (e: Exception) {
                Log.e(TAG, "Download failed", e)
                _state.value = ModelState.Error("Download failed: ${e.message}")
                throw e
            } finally {
                connection?.disconnect()
            }
        }
    }

    override suspend fun cancelDownload() {
        downloadCancelled = true
        _state.value = ModelState.Idle
    }

    override suspend fun load() {
        if (_state.value is ModelState.Ready) return
        if (!modelFile.exists()) {
            _state.value = ModelState.Error("Model not downloaded")
            return
        }

        _state.value = ModelState.Loading

        withContext(Dispatchers.IO) {
            try {
                val options = LlmInference.LlmInferenceOptions.builder()
                    .setModelPath(modelFile.absolutePath)
                    .setMaxTokens(512)
                    .setTemperature(0.6f)
                    .setTopK(40)
                    .setRandomSeed(42)
                    .build()

                inference = LlmInference.createFromOptions(context, options)
                _state.value = ModelState.Ready
                Log.i(TAG, "Model loaded and ready")
            } catch (e: Exception) {
                Log.e(TAG, "Model load failed", e)
                _state.value = ModelState.Error("Load failed: ${e.message}")
                throw e
            }
        }
    }

    override suspend fun unload() {
        try {
            inference?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing inference", e)
        }
        inference = null

        if (_state.value is ModelState.Ready || _state.value is ModelState.Loading) {
            _state.value = if (modelFile.exists()) ModelState.Downloaded else ModelState.Idle
        }
        Log.i(TAG, "Model unloaded")
    }

    override suspend fun delete() {
        unload()
        withContext(Dispatchers.IO) {
            modelFile.delete()
            File(modelDir, "$MODEL_FILENAME.tmp").delete()
        }
        _state.value = ModelState.Idle
        Log.i(TAG, "Model deleted")
    }

    override suspend fun generate(prompt: String, maxTokens: Int): String {
        val llm = inference ?: throw IllegalStateException("Model not loaded")

        return withContext(Dispatchers.IO) {
            try {
                llm.generateResponse(prompt)
            } catch (e: Exception) {
                Log.e(TAG, "Inference failed", e)
                throw e
            }
        }
    }
}
