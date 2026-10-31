package com.flightrisk.app.llm

import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.Base64
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * LLM backend that calls the Anthropic Messages API (Claude) for visual
 * reasoning. It sends the reference photo and the detected person's crop
 * (JPEG, base64) and, when the user typed one, the text description.
 *
 * Ports the prompt logic from `FlightRiskAgent.analyze_match()` and
 * `FlightRiskAgent.match_description()` in the Python codebase. The response
 * is parsed for the MATCH / CONFIDENCE / REASONING format the Python agent uses.
 *
 * Cloud use is controlled by [CloudReasoningGate], which reads the saved
 * opt-in, the selected provider and the key in [com.flightrisk.app.ground.SecretStore]
 * at call time:
 * - The gate is checked at the start of every call, before anything is
 *   encoded, and again immediately before the HTTP request. One local copy of
 *   the key is used for that second check and for the header, so the header is
 *   never empty and the backend keeps no key field.
 * - The backend has its own [OkHttpClient]. When the gate closes (opt-out,
 *   another provider selected, key removed) every call in flight is cancelled
 *   at once, and a request that was about to start is dropped before it sends.
 * - The mutable state (the generation counter and the calls in flight) is
 *   guarded by one lock.
 *
 * This is the only code in the app that sends images or text to a cloud
 * service; `CloudPathAuditTest` fails if another file starts to.
 *
 * @param gate Decides whether cloud use is allowed and yields the key.
 * @param isOnline Whether the device has validated internet.
 * @param apiUrl The Messages API endpoint. Tests point it at a loopback server.
 * @param model Claude model identifier.
 * @param timeoutMs HTTP connect, read, write and call timeout.
 * @param encodeImage Turns a bitmap into base64 JPEG. Tests replace it.
 * @param log Receives log lines, which never contain the key or the payload.
 */
class CloudClaudeLlmBackend(
    private val gate: CloudReasoningGate,
    private val isOnline: () -> Boolean,
    private val apiUrl: String = API_URL,
    private val model: String = DEFAULT_MODEL,
    timeoutMs: Long = 30_000,
    private val encodeImage: (Bitmap) -> String = ::jpegBase64,
    private val log: (String) -> Unit = ::androidLog,
) : LlmBackend {

    override val name: String = NAME

    /**
     * Dedicated client, so cancelling cloud calls never touches other traffic.
     * It never follows a redirect (OkHttp would re-send the body and keep the
     * `x-api-key` header across hosts, and could downgrade https to http) and
     * never re-sends a request: [SingleAttempt] refuses a second network
     * attempt within one call and strips `Retry-After`, which would otherwise
     * make OkHttp repeat the upload after a 503.
     */
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .writeTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .callTimeout(timeoutMs * 2, TimeUnit.MILLISECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .retryOnConnectionFailure(false)
        .addNetworkInterceptor(SingleAttempt)
        .build()

    /** Test hook run between the second gate check and registering the call. */
    @Volatile
    internal var beforeSend: (() -> Unit)? = null

    private val lock = Any()

    /** Incremented on every cancellation; a request prepared under an older value never starts. Guarded by [lock]. */
    private var generation = 0L

    /** Calls in flight. Guarded by [lock]. */
    private val inFlight = LinkedHashSet<Call>()

    private val gateListener = CloudGateListener { keyRemoved ->
        if (keyRemoved || !gate.isAllowedBySettings()) cancelAllRequests()
    }

    init {
        gate.addListener(gateListener)
    }

    /**
     * Main-safe: online, opt-in on, Cloud Claude selected and a key known to be
     * saved. A stale "saved" answer is harmless because every call reads the
     * key again through the gate.
     */
    override val isAvailable: Boolean
        get() = gate.isAllowedBySettings() && gate.keyState == ClaudeKeyState.FOUND && isOnline()

    /** Cancels every call in flight and any call that is about to start. */
    fun cancelAllRequests() {
        val calls = synchronized(lock) {
            generation++
            inFlight.toList().also { inFlight.clear() }
        }
        calls.forEach { it.cancel() }
        client.dispatcher.cancelAll()
    }

    /** Number of calls in flight, for tests. */
    internal fun inFlightCount(): Int = synchronized(lock) { inFlight.size }

    /** Stops listening to the gate and cancels everything. */
    fun close() {
        gate.removeListener(gateListener)
        cancelAllRequests()
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    override suspend fun analyzeMatch(
        referenceImage: Bitmap,
        candidateImage: Bitmap,
        description: String?,
    ): ReasoningResult = withContext(Dispatchers.IO) {
        // First gate check: nothing is encoded unless cloud use is allowed.
        if (!isOnline() || gate.keyIfOpen() == null) return@withContext BLOCKED

        val content = try {
            val refB64 = encodeImage(referenceImage)
            val candB64 = encodeImage(candidateImage)
            listOf(imageBlock(refB64), imageBlock(candB64), textBlock(analyzePrompt(description)))
        } catch (e: Exception) {
            return@withContext errorResult("Failed to encode images", e)
        }
        callApi(content)
    }

    override suspend fun describeMatch(
        candidateImage: Bitmap,
        description: String,
    ): ReasoningResult = withContext(Dispatchers.IO) {
        // First gate check: nothing is encoded unless cloud use is allowed.
        if (!isOnline() || gate.keyIfOpen() == null) return@withContext BLOCKED

        val content = try {
            val candB64 = encodeImage(candidateImage)
            listOf(imageBlock(candB64), textBlock(describePrompt(description)))
        } catch (e: Exception) {
            return@withContext errorResult("Failed to encode image", e)
        }
        callApi(content)
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    /** Sends one Messages API request and parses the structured answer. Blocking. */
    private fun callApi(contentBlocks: List<JsonObject>): ReasoningResult {
        val body = buildJsonObject {
            put("model", model)
            put("max_tokens", MAX_TOKENS)
            putJsonArray("messages") {
                addJsonObject {
                    put("role", "user")
                    put("content", JsonArray(contentBlocks))
                }
            }
        }.toString()

        // Read the generation first, so a cancellation from here on drops this request.
        val generationAtStart = synchronized(lock) { generation }

        // Second gate check, immediately before sending. This local value is the
        // only copy of the key: it is checked here and placed in the header.
        if (!isOnline()) return BLOCKED
        val key = gate.keyIfOpen() ?: return BLOCKED
        if (!isHeaderSafe(key)) {
            log("Cloud request not sent: the saved Claude key contains characters a header cannot carry")
            return ReasoningResult(isMatch = false, confidence = "error", reasoning = "The saved Claude API key is not valid")
        }

        val request = Request.Builder()
            .url(apiUrl)
            .header("content-type", "application/json")
            .header("x-api-key", key)
            .header("anthropic-version", API_VERSION)
            .post(body.toRequestBody(JSON))
            .tag(AttemptCounter::class.java, AttemptCounter())
            .build()
        val call = client.newCall(request)
        beforeSend?.invoke()

        val registered = synchronized(lock) {
            if (generation != generationAtStart) false else inFlight.add(call)
        }
        if (!registered) {
            call.cancel()
            return BLOCKED
        }

        return try {
            call.execute().use { response ->
                if (response.code != 200) {
                    // Redirects (3xx) are not followed and count as errors. The error
                    // body is not logged: it is not needed and could echo request data.
                    log("Claude API error ${response.code}")
                    return ReasoningResult(isMatch = false, confidence = "error", reasoning = "API error ${response.code}")
                }
                val text = response.body?.string().orEmpty()
                val contentArray = Json.parseToJsonElement(text).jsonObject.getValue("content").jsonArray
                val fullText = buildString {
                    for (element in contentArray) {
                        val block = element as? JsonObject ?: continue
                        if ((block["type"] as? JsonPrimitive)?.content == "text") {
                            append((block["text"] as? JsonPrimitive)?.content.orEmpty())
                        }
                    }
                }
                parseMatchResponse(fullText)
            }
        } catch (e: IOException) {
            if (call.isCanceled()) {
                log("Cloud request cancelled because cloud reasoning was turned off or the key was removed")
                BLOCKED
            } else {
                errorResult("Cloud request failed", e)
            }
        } catch (e: Exception) {
            errorResult("Could not read the Claude response", e)
        } finally {
            synchronized(lock) { inFlight.remove(call) }
        }
    }

    /**
     * Parse the structured MATCH / CONFIDENCE / REASONING response.
     *
     * Mirrors `FlightRiskAgent._parse_match_response()` from the Python codebase.
     */
    internal fun parseMatchResponse(text: String): ReasoningResult {
        var isMatch = false
        var confidence = "unknown"
        var reasoning = text

        for (line in text.split("\n")) {
            val upper = line.trim().uppercase()
            when {
                upper.startsWith("MATCH:") -> isMatch = "YES" in upper
                upper.startsWith("CONFIDENCE:") -> confidence = upper.substringAfter(":").trim().lowercase()
                upper.startsWith("REASONING:") -> reasoning = line.trim().substringAfter(":").trim()
            }
        }
        return ReasoningResult(isMatch = isMatch, confidence = confidence, reasoning = reasoning)
    }

    /** An error result with the exception type only; exception messages can carry request data. */
    private fun errorResult(what: String, e: Exception): ReasoningResult {
        log("$what (${e.javaClass.simpleName})")
        return ReasoningResult(isMatch = false, confidence = "error", reasoning = "$what (${e.javaClass.simpleName})")
    }

    override fun toString(): String = "CloudClaudeLlmBackend(model=$model, available=$isAvailable)"

    /** Counts the network attempts of one call. */
    private class AttemptCounter {
        val attempts = AtomicInteger()
    }

    /** Allows one network attempt per call and removes `Retry-After` from responses. */
    private object SingleAttempt : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val counter = chain.request().tag(AttemptCounter::class.java)
            if (counter != null && counter.attempts.incrementAndGet() > 1) {
                throw IOException("A cloud request is never sent twice")
            }
            val response = chain.proceed(chain.request())
            return if (response.header("Retry-After") != null) {
                response.newBuilder().removeHeader("Retry-After").build()
            } else {
                response
            }
        }
    }

    companion object {
        const val NAME = "claude"
        const val API_URL = "https://api.anthropic.com/v1/messages"
        const val DEFAULT_MODEL = "claude-sonnet-4-20250514"
        private const val API_VERSION = "2023-06-01"
        private const val MAX_TOKENS = 256
        private const val JPEG_QUALITY = 85
        private const val TAG = "CloudClaudeLlm"
        private val JSON = "application/json".toMediaType()

        /** Returned whenever the gate is closed or a request was cancelled by it. */
        val BLOCKED = ReasoningResult(
            isMatch = false,
            confidence = "unavailable",
            reasoning = "Cloud reasoning is not allowed right now (not opted in, not selected, no API key, or offline)",
        )

        /** The backend for a device: connectivity from [ConnectivityManager]. */
        fun forDevice(context: Context, gate: CloudReasoningGate): CloudClaudeLlmBackend {
            val app = context.applicationContext
            return CloudClaudeLlmBackend(gate = gate, isOnline = { hasValidatedInternet(app) })
        }

        internal fun analyzePrompt(description: String?): String = buildString {
            append("You are helping find a missing person. ")
            append("Image 1 is the reference photo of the person we are looking for. ")
            append("Image 2 is a person detected by a drone camera.\n\n")
            append("Compare the two people. Consider: clothing color/type, hair, ")
            append("build, height, backpack/accessories, and any distinguishing features.\n\n")
            if (!description.isNullOrBlank()) append("Additional description of the person: $description\n\n")
            append("Respond in this exact format:\n")
            append("MATCH: yes or no\n")
            append("CONFIDENCE: high, medium, or low\n")
            append("REASONING: one sentence explaining why")
        }

        internal fun describePrompt(description: String): String = buildString {
            append("You are helping find a missing person. ")
            append("The person's description: $description\n\n")
            append("Look at this image of a person detected by a drone camera. ")
            append("Does this person match the description above?\n\n")
            append("Consider: clothing color/type, hair color/style, approximate age, ")
            append("build, backpack/accessories, and any distinguishing features.\n\n")
            append("Respond in this exact format:\n")
            append("MATCH: yes or no\n")
            append("CONFIDENCE: high, medium, or low\n")
            append("REASONING: one sentence explaining why")
        }

        /** A header value must be visible ASCII; anything else is refused before OkHttp sees it. */
        internal fun isHeaderSafe(value: String): Boolean = value.isNotEmpty() && value.all { it in '!'..'~' }

        private fun imageBlock(base64Data: String): JsonObject = buildJsonObject {
            put("type", "image")
            putJsonObject("source") {
                put("type", "base64")
                put("media_type", "image/jpeg")
                put("data", base64Data)
            }
        }

        private fun textBlock(text: String): JsonObject = buildJsonObject {
            put("type", "text")
            put("text", text)
        }

        private fun jpegBase64(bitmap: Bitmap): String {
            val stream = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, stream)
            return Base64.getEncoder().encodeToString(stream.toByteArray())
        }

        private fun androidLog(message: String) {
            try {
                Log.w(TAG, message)
            } catch (_: RuntimeException) {
                // android.util.Log is a stub on the JVM.
            }
        }

        /** Whether the device has a validated internet connection. Main-safe. */
        fun hasValidatedInternet(context: Context): Boolean {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return false
            val network = cm.activeNetwork ?: return false
            val capabilities = cm.getNetworkCapabilities(network) ?: return false
            return capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        }
    }
}
