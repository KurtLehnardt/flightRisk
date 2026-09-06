package com.flightrisk.app.transport

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.TimeUnit

class GroundTransport(
    private val port: Int = 9000,
    private val token: String? = null,
    private val onDetectionMessage: ((DetectionMessage) -> Unit)? = null,
) {
    companion object {
        private const val TAG = "GroundTransport"
    }

    private val clients = CopyOnWriteArraySet<WebSocket>()
    private val authenticated = CopyOnWriteArraySet<WebSocket>()

    fun sendTarget(
        webSocket: WebSocket,
        reidEmbedding: FloatArray,
        faceEmbedding: FloatArray? = null,
    ) {
        val msg = JSONObject().apply {
            put("type", "set_target")
            put("reid_embedding", JSONArray().apply { for (v in reidEmbedding) put(v.toDouble()) })
            faceEmbedding?.let { emb ->
                put("face_embedding", JSONArray().apply { for (v in emb) put(v.toDouble()) })
            }
        }
        try {
            webSocket.send(msg.toString())
        } catch (e: Exception) {
            clients.remove(webSocket)
            authenticated.remove(webSocket)
        }
    }

    fun broadcastTarget(reidEmbedding: FloatArray, faceEmbedding: FloatArray? = null) {
        val targets = if (token != null) authenticated else clients
        for (ws in targets) {
            sendTarget(ws, reidEmbedding, faceEmbedding)
        }
    }

    fun broadcastStreamVideo(enabled: Boolean) {
        val msg = JSONObject().apply {
            put("type", "stream_video")
            put("enabled", enabled)
        }
        val targets = if (token != null) authenticated else clients
        for (ws in targets) {
            try {
                ws.send(msg.toString())
            } catch (e: Exception) {
                clients.remove(ws)
                authenticated.remove(ws)
            }
        }
    }

    fun createListener(): WebSocketListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            clients.add(webSocket)
            Log.i(TAG, "Edge client connected (total: ${clients.size})")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val json = JSONObject(text)

                if (token != null && webSocket !in authenticated) {
                    if (json.optString("type") == "auth" && json.optString("token") == token) {
                        authenticated.add(webSocket)
                        Log.i(TAG, "Client authenticated")
                    } else {
                        Log.w(TAG, "Rejecting unauthenticated client")
                        webSocket.close(4001, "unauthorized")
                        clients.remove(webSocket)
                    }
                    return
                }

                when (json.optString("type")) {
                    "detections" -> {
                        val msg = DetectionMessage.fromJson(json)
                        onDetectionMessage?.invoke(msg)
                    }
                    "set_target", "stream_video" -> { /* tolerate echoed commands */ }
                    else -> Log.w(TAG, "Unknown message type: ${json.optString("type")}")
                }
            } catch (e: Exception) {
                Log.w(TAG, "Malformed message: ${e.message}")
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            clients.remove(webSocket)
            authenticated.remove(webSocket)
            Log.i(TAG, "Edge client disconnected (remaining: ${clients.size})")
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            clients.remove(webSocket)
            authenticated.remove(webSocket)
            Log.w(TAG, "Client connection failed: ${t.message}")
        }
    }

    val clientCount: Int get() = clients.size
}
