package com.flightrisk.app.llm.local

/**
 * Metadata for a local LLM model.
 *
 * @property id Unique identifier (e.g. "gemma-2-2b-it-q4").
 * @property displayName Human-readable name for Settings UI.
 * @property sizeBytes Approximate download size in bytes.
 * @property minRamMb Minimum device RAM in MB for inference.
 * @property quantization Quantization scheme (e.g. "Q4_K_M", "int4").
 * @property description One-line description of the model.
 */
data class LocalModelInfo(
    val id: String,
    val displayName: String,
    val sizeBytes: Long,
    val minRamMb: Int,
    val quantization: String,
    val description: String,
)
