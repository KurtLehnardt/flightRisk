package com.flightrisk.app.privacy

import com.flightrisk.app.transport.GroundLinkText
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Source audit for appearance embeddings on the edge link. They describe how a
 * person looks, so they stay in memory on the way to the ground station: no
 * storage, recording, backup, log or cloud code may touch them, and the
 * disclosure must change before a model that makes them is wired in.
 */
class EdgeEmbeddingAuditTest {
    private val root: File = listOf(File("src/main/java"), File("app/src/main/java"), File("mobile/app/src/main/java"))
        .firstOrNull { File(it, "com/flightrisk/app").isDirectory }
        ?: error("main sources not found from ${File(".").absolutePath}")

    private fun sources(): Map<String, String> =
        root.walkTopDown().filter { it.isFile && it.extension == "kt" }
            .associate { it.relativeTo(root).invariantSeparatorsPath to it.readText() }

    private val embeddingApi = Regex("""\bEmbeddingProvider\b|\bEdgeEmbeddings\b|\bedgeEmbeddingProvider\b|\bReidEmbedding\b|\.reidEmbedding\b|\breidEmbedding\s*=""")

    @Test
    fun `only the edge link path handles appearance embeddings`() {
        val users = sources().filter { (_, text) -> embeddingApi.containsMatchIn(text) }.keys
        assertEquals(
            setOf(
                "com/flightrisk/app/vision/EmbeddingProvider.kt",
                "com/flightrisk/app/transport/BitmapFrames.kt",
                "com/flightrisk/app/transport/EdgePublisher.kt",
                "com/flightrisk/app/pipeline/DetectionPublisher.kt",
                "com/flightrisk/app/pipeline/SearchPipeline.kt",
            ),
            users,
        )
        // In particular nothing that stores, records, erases, reasons in the cloud or shows data on screen.
        assertTrue(users.none { p -> listOf("/persistence/", "/recording/", "/llm/", "/privacy/", "/ui/", "/ground/").any { it in p } })
    }

    @Test
    fun `no log call in the embedding path names an embedding or a vector`() {
        val files = listOf(
            "com/flightrisk/app/vision/EmbeddingProvider.kt",
            "com/flightrisk/app/transport/BitmapFrames.kt",
            "com/flightrisk/app/transport/EdgePublisher.kt",
            "com/flightrisk/app/pipeline/DetectionPublisher.kt",
        )
        val logCall = Regex("""(\blog\(|Log\.[a-z]\(|println\(|logger\.)[^\n]*(mbedding|vector|values|output)""", RegexOption.IGNORE_CASE)
        val offenders = files.filter { logCall.containsMatchIn(sources().getValue(it)) }
        assertTrue("Log calls naming an embedding: $offenders", offenders.isEmpty())
        // The embedding helpers do not log at all.
        assertTrue(!Regex("""\bLog\.|\blog\(""").containsMatchIn(sources().getValue("com/flightrisk/app/vision/EmbeddingProvider.kt")))
    }

    /** docs/privacy-policy.md, found from the test working directory (mobile/app) or the repository root. */
    private fun privacyPolicy(): String =
        listOf("../../docs/privacy-policy.md", "../docs/privacy-policy.md", "docs/privacy-policy.md")
            .map(::File).firstOrNull { it.isFile }?.readText()
            ?: error("docs/privacy-policy.md not found from ${File(".").absolutePath}")

    /**
     * Sentences of docs/privacy-policy.md that are true only while no
     * appearance embedding leaves the phone. Each must be rewritten before an
     * embedding provider is wired in. Whitespace is normalized before matching.
     */
    private val policyClaimsThatNoEmbeddingIsSent = listOf(
        // Section 2: on-device processing.
        "Face embeddings and appearance feature vectors never leave your phone under any setting",
        "No biometric templates or embeddings are transmitted",
        // Section 3: biometric data handling.
        "transmitted over the network, or shared with any party",
        "Biometric templates and embeddings are not stored beyond the active session and are not transmitted",
        "Templates and embeddings are processed ephemerally and locally under your sole control",
        "templates and embeddings stay on the device",
        // Section 5B: phone as an edge.
        "The ground station cannot yet match these detections to a search target",
    )

    private fun normalized(text: String) = text.replace(Regex("""\s+"""), " ")

    private val implementation = Regex("""\b(class|object)\b[^\n{=]*:[^\n{=]*\bEmbeddingProvider\s*<""")

    /** Main sources that give the edge link an embedding provider other than [NoEmbeddingProvider]. */
    private fun wiredProviders(): Set<String> {
        val pipelinePath = "com/flightrisk/app/pipeline/SearchPipeline.kt"
        val providerFile = "com/flightrisk/app/vision/EmbeddingProvider.kt"
        val wired = mutableSetOf<String>()
        for ((path, text) in sources()) {
            // An assignment of the pipeline's provider anywhere but its own default.
            if (path != pipelinePath && Regex("""edgeEmbeddingProvider\s*=""").containsMatchIn(text)) wired += path
            // Any implementation of the interface (a class or an object) besides NoEmbeddingProvider.
            if (path != providerFile && implementation.containsMatchIn(text)) wired += path
            // BitmapFrames handed something other than the pipeline's provider.
            if (Regex("""embeddingProvider\s*=\s*(?!edgeEmbeddingProvider\b|NoEmbeddingProvider\b)\w""").containsMatchIn(text)) wired += path
        }
        val pipeline = sources().getValue(pipelinePath)
        if (!Regex("""var edgeEmbeddingProvider: EmbeddingProvider<Bitmap> = NoEmbeddingProvider\b""").containsMatchIn(pipeline)) wired += pipelinePath
        return wired
    }

    @Test
    fun `the forbidden policy sentences are still in the policy, so the check below is not empty`() {
        // Nothing is sent today, so the policy may keep saying so. If it is reworded, update the list above.
        val policy = normalized(privacyPolicy())
        val missing = policyClaimsThatNoEmbeddingIsSent.filter { !policy.contains(it) }
        assertTrue(
            "The privacy policy no longer contains $missing. If embeddings are now sent, that is expected: " +
                "remove the sentence from the list. Otherwise update the list to the new wording.",
            missing.isEmpty() || wiredProviders().isNotEmpty(),
        )
    }

    @Test
    fun `before an embedding model is wired in, the disclosure, the notice and the privacy policy must say so`() {
        val wiring = wiredProviders()
        if (wiring.isEmpty()) {
            // Nothing is sent today: the disclosure may say the ground cannot match.
            assertTrue(GroundLinkText.DISCLOSURE.contains("cannot yet match these detections"))
            return
        }
        // A model is wired in: the person must be told before anything is sent.
        val told = GroundLinkText.DISCLOSURE.contains("appearance") &&
            PrivacyNoticeText.allText().contains("appearance") &&
            !GroundLinkText.DISCLOSURE.contains("cannot yet match")
        assertTrue("Embeddings are wired in $wiring but the disclosure and notice do not mention them", told)
        val policy = normalized(privacyPolicy())
        val stale = policyClaimsThatNoEmbeddingIsSent.filter { policy.contains(it) }
        assertTrue("Embeddings are wired in $wiring but docs/privacy-policy.md still says: $stale", stale.isEmpty())
    }

    @Test
    fun `the wiring check notices a provider`() {
        // The detector on the real sources finds nothing today ...
        assertEquals(emptySet<String>(), wiredProviders())
        // ... and each pattern matches the line a follow-up would write.
        assertTrue(Regex("""edgeEmbeddingProvider\s*=""").containsMatchIn("pipeline.edgeEmbeddingProvider = ClipProvider(ctx)"))
        assertTrue(implementation.containsMatchIn("class ClipProvider(c: Context) : EmbeddingProvider<Bitmap> {"))
        assertTrue(implementation.containsMatchIn("val p = object : EmbeddingProvider<Bitmap> {"))
        assertTrue(!implementation.containsMatchIn("var edgeEmbeddingProvider: EmbeddingProvider<Bitmap> = NoEmbeddingProvider"))
        assertTrue(
            Regex("""embeddingProvider\s*=\s*(?!edgeEmbeddingProvider\b|NoEmbeddingProvider\b)\w""")
                .containsMatchIn("embeddingProvider = clip,"),
        )
    }
}
