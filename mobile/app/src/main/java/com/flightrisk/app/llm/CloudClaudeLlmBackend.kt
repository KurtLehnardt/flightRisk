package com.flightrisk.app.llm

import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Base64
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * LLM backend that calls the Anthropic Messages API (Claude) for
 * visual reasoning.
 *
 * Ports the prompt logic from `FlightRiskAgent.analyze_match()` and
 * `FlightRiskAgent.match_description()` in the Python codebase, but
 * targets the Claude REST API instead of local Ollama.
 *
 * Images are JPEG-encoded and sent as base64 `image` content blocks.
 * The response is parsed for the structured MATCH / CONFIDENCE /
 * REASONING format that the Python agent also uses.
 *
 * @param context Android context for connectivity checks.
 * @param apiKey Anthropic API key. When null, [isAvailable] is false.
 * @param model Claude model identifier to use.
 * @param timeoutMs HTTP read/connect timeout in milliseconds.
 */
class CloudClaudeLlmBackend(
    private val context: Context,
    private val apiKey: String?,
    private val model: String = "claude-sonnet-4-20250514",
    private val timeoutMs: Int = 30_000,
) : LlmBackend {

    override val name: String = "claude"

    override val isAvailable: Boolean
        get() = !apiKey.isNullOrBlank() && hasInternet()

    companion object {
        private const val TAG = "CloudClaudeLlm"
        private const val API_URL = "https://api.anthropic.com/v1/messages"
        private const val API_VERSION = "2023-06-01"
        private const val MAX_TOKENS = 256
        private const val JPEG_QUALITY = 85
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    override suspend fun analyzeMatch(
        referenceImage: Bitmap,
        candidateImage: Bitmap,
        description: String?,
    ): ReasoningResult = withContext(Dispatchers.IO) {
        if (!isAvailable) {
            return@withContext ReasoningResult(
                isMatch = false,
                confidence = "unavailable",
                reasoning = "Claude API not available (no API key or no internet)",
            )
        }

        val refB64 = bitmapToBase64(referenceImage)
        val candB64 = bitmapToBase64(candidateImage)

        val prompt = buildString {
            append("You are helping find a missing person. ")
            append("Image 1 is the reference photo of the person we are looking for. ")
            append("Image 2 is a person detected by a drone camera.\n\n")
            append("Compare the two people. Consider: clothing color/type, hair, ")
            append("build, height, backpack/accessories, and any distinguishing features.\n\n")
            if (!description.isNullOrBlank()) {
                append("Additional description of the person: $description\n\n")
            }
            append("Respond in this exact format:\n")
            append("MATCH: yes or no\n")
            append("CONFIDENCE: high, medium, or low\n")
            append("REASONING: one sentence explaining why")
        }

        val contentBlocks = JSONArray().apply {
            put(imageBlock(refB64))
            put(imageBlock(candB64))
            put(textBlock(prompt))
        }

        callApi(contentBlocks)
    }

    override suspend fun describeMatch(
        candidateImage: Bitmap,
        description: String,
    ): ReasoningResult = withContext(Dispatchers.IO) {
        if (!isAvailable) {
            return@withContext ReasoningResult(
                isMatch = false,
                confidence = "unavailable",
                reasoning = "Claude API not available (no API key or no internet)",
            )
        }

        val candB64 = bitmapToBase64(candidateImage)

        val prompt = buildString {
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

        val contentBlocks = JSONArray().apply {
            put(imageBlock(candB64))
            put(textBlock(prompt))
        }

        callApi(contentBlocks)
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    /**
     * Send a request to the Anthropic Messages API and parse the
     * structured response.
     */
    private fun callApi(contentBlocks: JSONArray): ReasoningResult {
        val body = JSONObject().apply {
            put("model", model)
            put("max_tokens", MAX_TOKENS)
            put("messages", JSONArray().apply {
                put(JSONObject().apply {
                    put("role", "user")
                    put("content", contentBlocks)
                })
            })
        }

        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(API_URL).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("x-api-key", apiKey)
                setRequestProperty("anthropic-version", API_VERSION)
                doOutput = true
            }

            connection.outputStream.bufferedWriter().use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            if (responseCode != 200) {
                val errorBody = connection.errorStream?.bufferedReader()?.readText() ?: "unknown"
                Log.w(TAG, "API error $responseCode: $errorBody")
                return ReasoningResult(
                    isMatch = false,
                    confidence = "error",
                    reasoning = "API error $responseCode",
                )
            }

            val responseText = connection.inputStream.bufferedReader().readText()
            val responseJson = JSONObject(responseText)
            val contentArray = responseJson.getJSONArray("content")

            val fullText = buildString {
                for (i in 0 until contentArray.length()) {
                    val block = contentArray.getJSONObject(i)
                    if (block.getString("type") == "text") {
                        append(block.getString("text"))
                    }
                }
            }

            parseMatchResponse(fullText)
        } catch (e: Exception) {
            Log.e(TAG, "API call failed", e)
            ReasoningResult(
                isMatch = false,
                confidence = "error",
                reasoning = e.message ?: "Unknown error",
            )
        } finally {
            connection?.disconnect()
        }
    }

    /**
     * Parse the structured MATCH / CONFIDENCE / REASONING response.
     *
     * Mirrors `FlightRiskAgent._parse_match_response()` from the Python
     * codebase.
     */
    internal fun parseMatchResponse(text: String): ReasoningResult {
        var isMatch = false
        var confidence = "unknown"
        var reasoning = text

        for (line in text.split("\n")) {
            val upper = line.trim().uppercase()
            when {
                upper.startsWith("MATCH:") -> {
                    isMatch = "YES" in upper
                }
                upper.startsWith("CONFIDENCE:") -> {
                    confidence = upper.substringAfter(":").trim().lowercase()
                }
                upper.startsWith("REASONING:") -> {
                    reasoning = line.trim().substringAfter(":").trim()
                }
            }
        }

        return ReasoningResult(
            isMatch = isMatch,
            confidence = confidence,
            reasoning = reasoning,
        )
    }

    /** Encode a [Bitmap] as a base64 JPEG string. */
    private fun bitmapToBase64(bitmap: Bitmap): String {
        val stream = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, stream)
        return Base64.encodeToString(stream.toByteArray(), Base64.NO_WRAP)
    }

    /** Build a Messages API image content block. */
    private fun imageBlock(base64Data: String): JSONObject = JSONObject().apply {
        put("type", "image")
        put("source", JSONObject().apply {
            put("type", "base64")
            put("media_type", "image/jpeg")
            put("data", base64Data)
        })
    }

    /** Build a Messages API text content block. */
    private fun textBlock(text: String): JSONObject = JSONObject().apply {
        put("type", "text")
        put("text", text)
    }

    /** Check whether the device currently has internet connectivity. */
    private fun hasInternet(): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return false
        val network = cm.activeNetwork ?: return false
        val capabilities = cm.getNetworkCapabilities(network) ?: return false
        return capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
            capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    }
}
