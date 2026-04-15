package com.momoagent.mlp

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class AgentWebSocketClient(
    private val url: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onSocketOpened()
        fun onSocketMessage(text: String)
        fun onSocketClosed(reason: String)
        fun onSocketFailure(message: String)
    }

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .build()

    private var socket: WebSocket? = null

    fun connect() {
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                listener.onSocketOpened()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                listener.onSocketMessage(text)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                listener.onSocketClosed(reason.ifBlank { "closed ($code)" })
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val message = t.message ?: response?.message ?: "unknown websocket failure"
                listener.onSocketFailure(message)
            }
        })
    }

    fun sendAsk(message: String, withTts: Boolean): Boolean {
        val payload = JSONObject()
            .put("type", "ask")
            .put("message", message)
            .put("with_tts", withTts)
        return socket?.send(payload.toString()) == true
    }

    fun close() {
        socket?.close(1000, "client_close")
        socket = null
        client.dispatcher.executorService.shutdown()
        client.connectionPool.evictAll()
    }
}
