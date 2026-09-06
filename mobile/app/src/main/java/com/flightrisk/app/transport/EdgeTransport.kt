package com.flightrisk.app.transport

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlin.math.min
import kotlin.math.pow

class EdgeTransport(
    private val wsUrl: String = "ws://localhost:9000",
    private val token: String? = null,
    private val maxRetries: Int? = null,
    private val baseDelay: Long = 1000,
    private val maxDelay: Long = 30_000,
) {
    companion object {
        private const val TAG = "EdgeTransport"
    }

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val lock = ReentrantLock()

    @Volatile
    var connected: Boolean = false
        private set

    private var webSocket: WebSocket? = null
    private var retryCount = 0

    @Volatile
    private var shouldReconnect = false

    private var reconnectThread: Thread? = null
    private var onMessageCallback: ((JSONObject) -> Unit)? = null

    fun setOnMessage(callback: (JSONObject) -> Unit) {
        onMessageCallback = callback
    }

    fun connect() {
        lock.withLock {
            if (connected) return
            connectLocked()
        }
    }

    private fun connectLocked() {
        val request = Request.Builder().url(wsUrl).build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                shouldReconnect = true
                retryCount = 0
                Log.i(TAG, "Connected to $wsUrl")

                if (token != null) {
                    val auth = JSONObject().apply {
                        put("type", "auth")
                        put("token", token)
                    }
                    webSocket.send(auth.toString())
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    onMessageCallback?.invoke(json)
                } catch (e: Exception) {
                    Log.w(TAG, "Malformed message: ${e.message}")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "Connection failed: ${t.message}")
                connected = false
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "Connection closed: $code $reason")
                connected = false
                scheduleReconnect()
            }
        })
    }

    fun sendDetections(msg: DetectionMessage) {
        if (!connected) {
            scheduleReconnect()
            return
        }
        try {
            webSocket?.send(msg.toJson().toString())
        } catch (e: Exception) {
            Log.w(TAG, "Send failed: ${e.message}")
            connected = false
            scheduleReconnect()
        }
    }

    fun disconnect() {
        shouldReconnect = false
        reconnectThread?.interrupt()
        reconnectThread = null

        lock.withLock {
            webSocket?.close(1000, "disconnect")
            webSocket = null
            connected = false
        }
        Log.i(TAG, "Disconnected")
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect) return
        if (reconnectThread?.isAlive == true) return

        reconnectThread = Thread {
            while (shouldReconnect && (maxRetries == null || retryCount < maxRetries)) {
                val delay = min(baseDelay * 2.0.pow(retryCount.toDouble()).toLong(), maxDelay)
                Log.i(TAG, "Reconnecting in ${delay}ms (attempt ${retryCount + 1})")

                try {
                    Thread.sleep(delay)
                } catch (_: InterruptedException) {
                    return@Thread
                }

                if (!shouldReconnect) return@Thread

                lock.withLock {
                    try {
                        connectLocked()
                    } catch (e: Exception) {
                        retryCount++
                        Log.w(TAG, "Reconnect failed: ${e.message}")
                        return@withLock
                    }
                }

                // Wait a bit and check if connected
                try { Thread.sleep(1000) } catch (_: InterruptedException) { return@Thread }
                if (connected) {
                    Log.i(TAG, "Reconnected successfully")
                    return@Thread
                }
                retryCount++
            }

            if (shouldReconnect) {
                Log.e(TAG, "Giving up reconnecting after $retryCount attempts")
            }
        }.also { it.isDaemon = true; it.start() }
    }
}
