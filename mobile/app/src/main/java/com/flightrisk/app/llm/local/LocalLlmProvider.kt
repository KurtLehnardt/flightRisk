package com.flightrisk.app.llm.local

import kotlinx.coroutines.flow.StateFlow

/**
 * Model-agnostic interface for on-device LLM inference.
 *
 * Implementations wrap a specific runtime (MediaPipe, llama.cpp,
 * ONNX GenAI, etc.) and a specific model (Gemma, Llama, Phi, etc.).
 * The rest of the app interacts only with this interface, so swapping
 * models or runtimes requires no changes outside the provider.
 *
 * Lifecycle: [download] -> [load] -> [generate] -> [unload] -> [delete].
 * Providers must be safe to call from any coroutine context.
 */
interface LocalLlmProvider {

    /** Static metadata about the model this provider serves. */
    val modelInfo: LocalModelInfo

    /** Observable lifecycle state. */
    val state: StateFlow<ModelState>

    /** Whether the model is loaded and ready for inference. */
    val isReady: Boolean

    /**
     * Download model weights to device storage.
     *
     * @param onProgress Called with fraction complete (0.0 to 1.0).
     * @throws Exception on network or disk errors.
     */
    suspend fun download(onProgress: (Float) -> Unit = {})

    /** Cancel an in-progress download. */
    suspend fun cancelDownload()

    /**
     * Load the downloaded model into memory for inference.
     *
     * @throws Exception if model files are missing or corrupt.
     */
    suspend fun load()

    /** Unload the model from memory without deleting files. */
    suspend fun unload()

    /**
     * Delete downloaded model files from disk.
     * Calls [unload] first if the model is loaded.
     */
    suspend fun delete()

    /**
     * Run text generation.
     *
     * @param prompt Full prompt including any chat template markers.
     * @param maxTokens Maximum tokens to generate.
     * @return Generated text.
     * @throws Exception if model is not ready or inference fails.
     */
    suspend fun generate(prompt: String, maxTokens: Int): String
}
