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

    @Test
    fun `the app wires no embedding model yet, so the disclosure may say the ground cannot match`() {
        val pipeline = sources().getValue("com/flightrisk/app/pipeline/SearchPipeline.kt")
        assertTrue(Regex("""var edgeEmbeddingProvider: EmbeddingProvider<Bitmap> = NoEmbeddingProvider""").containsMatchIn(pipeline))
        val wiring = sources().filter { (path, text) ->
            path != "com/flightrisk/app/pipeline/SearchPipeline.kt" && Regex("""edgeEmbeddingProvider\s*=""").containsMatchIn(text)
        }.keys
        if (wiring.isEmpty()) {
            assertTrue(GroundLinkText.DISCLOSURE.contains("cannot yet match these detections"))
        } else {
            // A model is wired in: the person must be told before anything is sent.
            val told = GroundLinkText.DISCLOSURE.contains("appearance") &&
                PrivacyNoticeText.allText().contains("appearance")
            assertTrue("Embeddings are wired in $wiring but the disclosure and notice do not mention them", told)
        }
    }
}
