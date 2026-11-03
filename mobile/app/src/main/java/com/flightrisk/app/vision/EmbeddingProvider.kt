package com.flightrisk.app.vision

import com.flightrisk.contract.ReidEmbedding

/**
 * Makes the appearance (ReID) embedding of one person crop, for the phone as
 * an edge of a ground station (`docs/mobile-contract.md`, "Phone as an edge").
 *
 * The ground compares each embedding with the target embedding it made from
 * the reference photo, so a provider is useful only when it runs the ground's
 * model: [modelId] must equal [ReidEmbedding.MODEL_ID]. A provider that names
 * another model is never run for the edge link and its vectors are never sent.
 *
 * The app does not ship such a model yet, so the app uses [NoEmbeddingProvider]
 * and every `reid_embedding` it sends is null. `mobile/README.md` ("Appearance
 * embeddings for the ground") says what model a provider needs.
 *
 * Rules for an implementation:
 * - [embed] runs on the edge publisher's worker thread, never on the camera
 *   pipeline's thread, and only while the ground station link is on and
 *   connected (the user accepted its disclosure, and the token rules passed).
 *   A reconnect starts a new worker while the old one may still finish, so
 *   [embed] must be safe to call from two threads at once.
 * - It returns the raw model output (any length is checked by the caller,
 *   which also L2-normalizes it), or null when it cannot make one.
 * - The input and the vector stay in memory: never write them to a file, the
 *   database, a recording, a backup or a log, and never hand them to the cloud
 *   reasoning path.
 */
interface EmbeddingProvider<in T> {
    /** The model the vectors come from, in the form of [ReidEmbedding.MODEL_ID]. */
    val modelId: String

    /** The embedding of [crop], or null when it cannot be made. */
    fun embed(crop: T): FloatArray?
}

/** No embedding model: every embedding is null and no work is done. The app's default. */
object NoEmbeddingProvider : EmbeddingProvider<Any?> {
    override val modelId: String = "none"

    override fun embed(crop: Any?): FloatArray? = null

    override fun toString(): String = "NoEmbeddingProvider"
}

/**
 * Prepares the appearance embeddings of one frame's detections for the edge
 * link. Nothing here knows about bitmaps, so it is tested on the JVM.
 */
object EdgeEmbeddings {
    /** True when [provider] runs the ground's model, so its vectors may be computed and sent. */
    fun usable(provider: EmbeddingProvider<*>): Boolean =
        provider !== NoEmbeddingProvider && provider.modelId == ReidEmbedding.MODEL_ID

    /**
     * One lazy embedding per detection. Only the [max] most confident
     * detections get one, and only when [provider] is [usable]: for those,
     * [prepare] runs now, on the caller's thread (it should make a small copy
     * of the crop), and the returned function later runs [provider] on that
     * copy, checks the result with [ReidEmbedding.toWire] and calls [release]
     * on the copy. Every other detection gets a function that returns null
     * and does no work.
     *
     * A returned function returns null when the provider has no vector, and
     * throws [IllegalArgumentException] (naming no value) when the vector is
     * not a valid embedding, or whatever the provider threw. It runs the
     * provider at most once.
     */
    fun <D, T> lazyEmbeddings(
        detections: List<D>,
        confidence: (D) -> Double,
        max: Int,
        provider: EmbeddingProvider<T>,
        prepare: (D) -> T?,
        release: (T) -> Unit = {},
    ): List<() -> List<Double>?> {
        val none: () -> List<Double>? = { null }
        if (max <= 0 || detections.isEmpty() || !usable(provider)) return List(detections.size) { none }
        val owners = detections.indices.sortedByDescending { confidence(detections[it]) }.take(max).toSet()
        return detections.mapIndexed { i, d ->
            if (i !in owners) return@mapIndexed none
            val input = prepare(d) ?: return@mapIndexed none
            once {
                try {
                    provider.embed(input)?.let(ReidEmbedding::toWire)
                } finally {
                    release(input)
                }
            }
        }
    }

    private fun once(block: () -> List<Double>?): () -> List<Double>? {
        val lock = Any()
        var done = false
        return {
            synchronized(lock) {
                if (done) {
                    null
                } else {
                    done = true
                    block()
                }
            }
        }
    }
}
