package com.flightrisk.app.llm

import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
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
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

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
 * @param model Claude model identifier; [DEFAULT_MODEL] unless a test sets it.
 * @param timeoutMs HTTP connect and write timeout.
 * @param readTimeoutMs HTTP read timeout. The request is not streamed, so the server sends
 *   nothing until the whole answer is generated; this must cover the full generation.
 * @param callTimeoutMs Limit for the whole call, from connect to the last byte.
 * @param nowMs Monotonic clock in milliseconds, for the failure breaker. Tests replace it.
 * @param encodeImage Turns a bitmap into base64 JPEG (downscaled first). Tests replace it.
 * @param log Receives log lines, which never contain the key or the payload.
 */
class CloudClaudeLlmBackend(
    private val gate: CloudReasoningGate,
    private val isOnline: () -> Boolean,
    private val apiUrl: String = API_URL,
    private val model: String = DEFAULT_MODEL,
    timeoutMs: Long = 30_000,
    readTimeoutMs: Long = READ_TIMEOUT_MS,
    callTimeoutMs: Long = CALL_TIMEOUT_MS,
    private val nowMs: () -> Long = { System.nanoTime() / 1_000_000 },
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
        .readTimeout(readTimeoutMs, TimeUnit.MILLISECONDS)
        .writeTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .callTimeout(callTimeoutMs, TimeUnit.MILLISECONDS)
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

    /** Consecutive failed requests. Guarded by [lock]. */
    private var consecutiveFailures = 0

    /** [nowMs] value at which the breaker closes again, or 0 while it is closed. Guarded by [lock]. */
    private var breakerOpenUntil = 0L

    /**
     * The last problem to show the user (HTTP status, rejected key), or null after a success.
     * Held in a reference, not a String field, so the audit test's "no key field" check
     * stays strict: this text never holds the key.
     */
    private val lastProblem = java.util.concurrent.atomic.AtomicReference<String?>(null)

    /** Called (on the calling thread, outside the lock) when [problem] or [breakerOpen] changed. */
    @Volatile
    var healthListener: (() -> Unit)? = null

    /** Why recent requests failed, for the reasoning notice. Never contains the key or request data. */
    val problem: String? get() = lastProblem.get()

    /**
     * True while the breaker is open: after [FAILURE_LIMIT] failures in a row no request is
     * sent for [COOL_OFF_MS], so a dead key or an outage does not keep uploading images.
     */
    val breakerOpen: Boolean
        get() = synchronized(lock) { breakerOpenUntil != 0L && nowMs() < breakerOpenUntil }

    /** Cached base64 of the last reference photo, so it is encoded once per search. Guarded by [lock]. */
    private var cachedReference: Pair<java.lang.ref.WeakReference<Bitmap>, String>? = null

    private val gateListener = CloudGateListener { keyRemoved ->
        if (keyRemoved || !gate.isAllowedBySettings()) cancelAllRequests()
    }

    /** A key was saved or removed: failures recorded for the old key say nothing about the new one. */
    private val keyChangeListener = Runnable { resetHealth() }

    init {
        gate.addListener(gateListener)
        gate.addKeyChangeListener(keyChangeListener)
    }

    /**
     * Main-safe: online, opt-in on, Cloud Claude selected and a key known to be
     * saved. A stale "saved" answer is harmless because every call reads the
     * key again through the gate.
     */
    override val isAvailable: Boolean
        get() = gate.isAllowedBySettings() && gate.keyState == ClaudeKeyState.FOUND && !breakerOpen && isOnline()

    /** Cancels every call in flight and any call that is about to start. */
    fun cancelAllRequests() {
        val calls = synchronized(lock) {
            generation++
            // The reference photo belongs to the search that was just stopped.
            cachedReference = null
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
        gate.removeKeyChangeListener(keyChangeListener)
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
        if (!isOnline() || breakerOpen || gate.keyIfOpen() == null) return@withContext BLOCKED

        val content = try {
            val refB64 = encodeReference(referenceImage)
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
        if (!isOnline() || breakerOpen || gate.keyIfOpen() == null) return@withContext BLOCKED

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

    /** Encodes the reference photo once for as long as the same bitmap is used (one search). */
    private fun encodeReference(bitmap: Bitmap): String {
        synchronized(lock) {
            val cached = cachedReference
            if (cached != null) {
                if (cached.first.get() === bitmap) return cached.second
                // The bitmap it was made for is gone (or replaced): drop the encoded copy too.
                cachedReference = null
            }
        }
        val encoded = encodeImage(bitmap)
        synchronized(lock) { cachedReference = java.lang.ref.WeakReference(bitmap) to encoded }
        return encoded
    }

    /** Forgets the failure state (notice, failure count, breaker) because the saved key changed. */
    private fun resetHealth() {
        synchronized(lock) {
            consecutiveFailures = 0
            breakerOpenUntil = 0L
            lastProblem.set(null)
            cachedReference = null
        }
        healthListener?.invoke()
    }

    /**
     * Records one failed request and opens the breaker after [FAILURE_LIMIT] in a row.
     * After the cool-off the breaker is half-open: the count stays at one below the limit,
     * so a single failed probe opens it again at once, while a success closes it.
     */
    private fun recordFailure(problemText: String) {
        synchronized(lock) {
            consecutiveFailures++
            lastProblem.set(problemText)
            if (consecutiveFailures >= FAILURE_LIMIT) {
                breakerOpenUntil = nowMs() + COOL_OFF_MS
                consecutiveFailures = FAILURE_LIMIT - 1
                log("Cloud reasoning paused for ${COOL_OFF_MS / 1000} s after $FAILURE_LIMIT failed requests in a row")
            }
        }
        healthListener?.invoke()
    }

    private fun recordSuccess() {
        val changed = synchronized(lock) {
            val had = lastProblem.get() != null || consecutiveFailures != 0 || breakerOpenUntil != 0L
            consecutiveFailures = 0
            breakerOpenUntil = 0L
            lastProblem.set(null)
            had
        }
        if (changed) healthListener?.invoke()
    }

    /** Sends one Messages API request and parses the structured answer. Blocking. */
    private suspend fun callApi(contentBlocks: List<JsonObject>): ReasoningResult {
        val body = requestBody(model, contentBlocks)

        // Read the generation first, so a cancellation from here on drops this request.
        val generationAtStart = synchronized(lock) { generation }

        // Second gate check, immediately before sending. This local value is the
        // only copy of the key: it is checked here and placed in the header. The key
        // generation is read first, so a 401 can be tied to the key that was sent.
        if (!isOnline()) return BLOCKED
        val keyGeneration = gate.keyGeneration
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
            call.await().use { response ->
                if (response.code != 200) {
                    // Redirects (3xx) are not followed and count as errors. The error
                    // body is not logged: it is not needed and could echo request data.
                    log("Claude API error ${response.code}")
                    val shown = when (response.code) {
                        401, 403 -> {
                            // The key is wrong, revoked or not allowed. Stop using it until the
                            // user saves a new one; the status code is shown, never the key.
                            if (!gate.markKeyInvalid(keyGeneration)) {
                                // The key was saved or removed while this request was in flight, so the
                                // answer is about a key that is no longer the saved one.
                                log("Ignored a late HTTP ${response.code} for a key that has since changed")
                                return BLOCKED
                            }
                            recordFailure("Claude rejected the saved API key (HTTP ${response.code}). Save a valid key in Settings.")
                            "Claude rejected the saved API key (HTTP ${response.code})"
                        }
                        in 400..499 -> {
                            recordFailure("Claude refused the request (HTTP ${response.code}).")
                            "API error ${response.code}"
                        }
                        else -> {
                            recordFailure("Claude is not answering (HTTP ${response.code}).")
                            "API error ${response.code}"
                        }
                    }
                    return ReasoningResult(isMatch = false, confidence = "error", reasoning = shown)
                }
                parseApiResponse(response.body?.string().orEmpty()).also { result ->
                    // A refusal is an answer about this one image, not a sign the service is down.
                    if (result.confidence == "error" && result.reasoning != REFUSAL_TEXT) {
                        recordFailure(result.reasoning)
                    } else {
                        recordSuccess()
                    }
                }
            }
        } catch (e: CancellationException) {
            // The search was stopped: the call is cancelled by [await], nothing to record.
            throw e
        } catch (e: IOException) {
            if (call.isCanceled()) {
                log("Cloud request cancelled because cloud reasoning was turned off or the key was removed")
                BLOCKED
            } else {
                recordFailure("The cloud request failed (${e.javaClass.simpleName}).")
                errorResult("Cloud request failed", e)
            }
        } catch (e: Exception) {
            recordFailure("The Claude response could not be read.")
            errorResult("Could not read the Claude response", e)
        } finally {
            synchronized(lock) { inFlight.remove(call) }
        }
    }

    /**
     * Reads a Messages API response body. The stop reason is checked before the
     * content: a refusal is an HTTP 200 whose content must not be read as an
     * answer. The answer is the text of the `text` blocks only, in order; other
     * blocks (`thinking`, which is empty by default) are skipped by type.
     */
    internal fun parseApiResponse(body: String): ReasoningResult {
        val root = Json.parseToJsonElement(body).jsonObject
        val stopReason = (root["stop_reason"] as? JsonPrimitive)?.content
        if (stopReason == "refusal") {
            // The category (for example "general_harms") says nothing about the request data.
            val category = ((root["stop_details"] as? JsonObject)?.get("category") as? JsonPrimitive)?.content
            log("Claude declined the request (refusal, category ${category ?: "none"})")
            return ReasoningResult(isMatch = false, confidence = "error", reasoning = REFUSAL_TEXT)
        }
        val contentArray = root.getValue("content").jsonArray
        val fullText = buildString {
            for (element in contentArray) {
                val block = element as? JsonObject ?: continue
                if ((block["type"] as? JsonPrimitive)?.content == "text") {
                    append((block["text"] as? JsonPrimitive)?.content.orEmpty())
                }
            }
        }
        if (stopReason == "max_tokens" && fullText.isBlank()) {
            log("Claude response stopped at max_tokens before any answer")
            return ReasoningResult(isMatch = false, confidence = "error", reasoning = "Claude did not finish its answer")
        }
        if (fullText.isBlank()) {
            // An answer with no text (for example end_turn after thinking only) is not "no match".
            log("Claude response had no text (stop_reason ${stopReason ?: "none"})")
            return ReasoningResult(isMatch = false, confidence = "error", reasoning = "Claude returned no answer")
        }
        return parseMatchResponse(fullText)
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

    /** Suspends until the response head arrives; cancelling the coroutine cancels the HTTP call. */
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private suspend fun Call.await(): Response = suspendCancellableCoroutine { continuation ->
        enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                continuation.resume(response) { response.close() }
            }

            override fun onFailure(call: Call, e: IOException) {
                continuation.resumeWithException(e)
            }
        })
        continuation.invokeOnCancellation { cancel() }
    }

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
        /**
         * The Claude model the app uses, the one place it is named. Claude Sonnet 5.5
         * replaces claude-sonnet-4-20250514, which is deprecated.
         */
        const val DEFAULT_MODEL = "claude-sonnet-5-5"
        private const val API_VERSION = "2023-06-01"

        /**
         * The answer is three short lines, but on Claude Sonnet 5.5 thinking is adaptive
         * and counts toward max_tokens even when its text is not returned. At high effort
         * the thinking can be long, so the limit leaves generous room for it.
         */
        internal const val MAX_TOKENS = 8192

        /**
         * Thinking effort, the user's choice for this app: high. Sent in `output_config`.
         */
        internal const val EFFORT = "high"

        /**
         * The request is not streamed, so the server sends nothing until the whole answer
         * (thinking included) is generated. The read timeout must cover that: 120 s, and
         * the whole call is capped at 150 s. Calls run on the existing background worker,
         * which drains its queue one item at a time, so a slow call delays later checks
         * but never blocks the camera or the UI.
         */
        internal const val READ_TIMEOUT_MS = 120_000L
        internal const val CALL_TIMEOUT_MS = 150_000L

        /**
         * After this many failed requests in a row the breaker opens. The desktop agent's
         * breaker is in PR #191, which is not merged yet, so this is not claimed to match it.
         */
        internal const val FAILURE_LIMIT = 3

        /** How long the breaker stays open. */
        internal const val COOL_OFF_MS = 60_000L

        /** The longest edge of an image sent to Anthropic, in pixels (the same as the desktop agent). */
        internal const val MAX_IMAGE_EDGE = 768

        /** The reasoning text for a refusal; a refusal does not count as a failure for the breaker. */
        private const val REFUSAL_TEXT = "Claude declined to compare these images"

        // No server-side refusal fallback (the `server-side-fallback-2026-07-01` beta and
        // `"fallbacks": "default"` that the desktop agent sends for this model). The
        // desktop is updated with the server, but a shipped app is not: if Anthropic
        // retired the beta, every request from an old install would fail until the user
        // updated. For Claude Sonnet 5.5 the default fallback also retries only `cyber`
        // and `frontier_llm` declines, which comparing two photos of people by clothing
        // should not trigger. A decline is reported as an error and counted in the
        // notice instead. Revisit if real traffic shows declines.
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

        /**
         * The Messages API request body. It follows the rules for Claude Sonnet 5.5:
         * one user turn and no assistant prefill, no `temperature`, `top_p` or
         * `top_k` (a non-default value is rejected), no `thinking` field (thinking
         * is adaptive by default; `disabled` is rejected), and the effort set in
         * `output_config`. Checked against the Messages API: `output_config.effort`
         * takes `high` for this model (also its default), and 8192 output tokens is
         * within what a non-streamed request may ask for.
         */
        internal fun requestBody(model: String, contentBlocks: List<JsonObject>): String = buildJsonObject {
            put("model", model)
            put("max_tokens", MAX_TOKENS)
            putJsonObject("output_config") {
                put("effort", EFFORT)
            }
            putJsonArray("messages") {
                addJsonObject {
                    put("role", "user")
                    put("content", JsonArray(contentBlocks))
                }
            }
        }.toString()

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

        /**
         * The size to send for a bitmap: the long edge at most [MAX_IMAGE_EDGE], the shape
         * kept. A smaller image is sent as it is. Less data leaves the phone and the
         * request is smaller, as on the desktop.
         */
        internal fun scaledSize(width: Int, height: Int): Pair<Int, Int> {
            val longEdge = maxOf(width, height)
            if (longEdge <= MAX_IMAGE_EDGE || width <= 0 || height <= 0) return width to height
            val scale = MAX_IMAGE_EDGE.toDouble() / longEdge
            return maxOf(1, Math.round(width * scale).toInt()) to maxOf(1, Math.round(height * scale).toInt())
        }

        private fun jpegBase64(bitmap: Bitmap): String {
            val (w, h) = scaledSize(bitmap.width, bitmap.height)
            val scaled = if (w != bitmap.width || h != bitmap.height) Bitmap.createScaledBitmap(bitmap, w, h, true) else bitmap
            val stream = ByteArrayOutputStream()
            try {
                scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, stream)
            } finally {
                if (scaled !== bitmap) scaled.recycle()
            }
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
