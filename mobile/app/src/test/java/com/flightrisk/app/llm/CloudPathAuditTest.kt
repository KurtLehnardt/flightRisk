package com.flightrisk.app.llm

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Source audit: every file in the app that can open a network connection is
 * listed here with where it connects, and the only one that may reach a cloud
 * service is [CloudClaudeLlmBackend], which goes through [CloudReasoningGate].
 * A new network path fails this test until it is reviewed and listed.
 */
class CloudPathAuditTest {

    private val root: File = listOf(File("src/main/java"), File("app/src/main/java"), File("mobile/app/src/main/java"))
        .firstOrNull { File(it, "com/flightrisk/app").isDirectory }
        ?: error("main sources not found from ${File(".").absolutePath}")

    /** Files allowed to open connections, and where they connect. */
    private val allowed = mapOf(
        "com/flightrisk/app/llm/CloudClaudeLlmBackend.kt" to
            "CLOUD: api.anthropic.com, only through CloudReasoningGate (opt-in, Cloud Claude selected, key in SecretStore)",
        "com/flightrisk/app/llm/local/GemmaMediaPipeProvider.kt" to
            "Model download from storage.googleapis.com when the user taps Download; sends no photo, crop, description or location",
        "com/flightrisk/app/ground/RawWebSocket.kt" to "The user's own ground station (plain http/ws on the local network)",
        "com/flightrisk/app/ground/SocketIoPort.kt" to "The user's own ground station (plain http/ws on the local network)",
        "com/flightrisk/app/ground/DashboardRestClient.kt" to "The user's own ground station (plain http on the local network)",
        "com/flightrisk/app/transport/EdgeSocketPort.kt" to
            "The user's own ground station edge port (plain ws on the local network), only while the opt-in link is on",
        "com/flightrisk/app/drone/TelloConnection.kt" to "Tello drone UDP commands on its own Wi-Fi",
        "com/flightrisk/app/drone/TelloFrameSource.kt" to "Tello drone UDP video on its own Wi-Fi",
    )

    private val networkApi = Regex(
        """HttpURLConnection|HttpsURLConnection|openConnection\(|openStream\(|OkHttpClient|\.newCall\(|newWebSocket\(|""" +
            """java\.net\.Socket\b|\bSocket\(|DatagramSocket|SocketChannel|URLConnection|WebView|DownloadManager""",
    )

    private fun sources(): Map<String, String> =
        root.walkTopDown().filter { it.isFile && it.extension == "kt" }
            .associate { it.relativeTo(root).invariantSeparatorsPath to it.readText() }

    @Test
    fun `every file that opens a connection is reviewed and listed`() {
        val users = sources().filter { (_, text) -> networkApi.containsMatchIn(text) }.keys
        val unlisted = users - allowed.keys
        assertTrue("Unreviewed network code: $unlisted", unlisted.isEmpty())
        assertTrue("The cloud backend must be among the network users", "com/flightrisk/app/llm/CloudClaudeLlmBackend.kt" in users)
    }

    @Test
    fun `only the gated cloud backend names a cloud reasoning host`() {
        val cloudHost = Regex("""anthropic\.com|openai\.com|generativelanguage\.googleapis|googleapis\.com/v1|api\.cohere|mistral\.ai""")
        val files = sources().filter { (_, text) -> cloudHost.containsMatchIn(text) }.keys
        assertEquals(setOf("com/flightrisk/app/llm/CloudClaudeLlmBackend.kt"), files)
    }

    @Test
    fun `the only https endpoints are the gated Claude API and the model download`() {
        val https = Regex(""""https://[^"\s]+""")
        val hosts = sources().flatMap { (path, text) -> https.findAll(text).map { path to it.value.trim('"') }.toList() }
        val expected = setOf(
            "com/flightrisk/app/llm/CloudClaudeLlmBackend.kt" to "https://api.anthropic.com/v1/messages",
        )
        val others = hosts.filterNot { it in expected || it.first == "com/flightrisk/app/llm/local/GemmaMediaPipeProvider.kt" }
        assertTrue("Unexpected https endpoints: $others", others.isEmpty())
    }

    @Test
    fun `the cloud backend cannot be built without a gate and keeps no key field`() {
        val ctor = CloudClaudeLlmBackend::class.java.constructors
        assertTrue(ctor.all { c -> c.parameterTypes.contains(CloudReasoningGate::class.java) })
        val stringFields = CloudClaudeLlmBackend::class.java.declaredFields
            .filter { it.type == String::class.java && !java.lang.reflect.Modifier.isStatic(it.modifiers) }
            .map { it.name }
            .toSet()
        assertEquals(setOf("name", "apiUrl", "model"), stringFields)
    }

    @Test
    fun `nothing outside the reasoning code reads or writes the Claude key or the old key name`() {
        val offenders = sources().filter { (path, text) ->
            val touches = Regex("""SecretName\.CLAUDE_API_KEY|"FLIGHTRISK_(CLAUDE_)?API_KEY"""").containsMatchIn(text)
            touches && path !in setOf(
                "com/flightrisk/app/ground/SecretStore.kt",
                "com/flightrisk/app/llm/CloudReasoningGate.kt",
                "com/flightrisk/app/llm/ReasoningSettings.kt",
            )
        }.keys
        assertTrue("Claude key accessed outside the gate: $offenders", offenders.isEmpty())
    }

    @Test
    fun `every LlmBackend that is not on-device is the gated cloud backend`() {
        val backends = sources().filter { (_, text) -> Regex(""":\s*LlmBackend\b""").containsMatchIn(text) }.keys
        val networked = backends.filter { networkApi.containsMatchIn(sources().getValue(it)) }
        assertEquals(listOf("com/flightrisk/app/llm/CloudClaudeLlmBackend.kt"), networked)
    }
}
