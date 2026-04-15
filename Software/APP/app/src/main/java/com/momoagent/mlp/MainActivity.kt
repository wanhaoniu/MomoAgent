package com.momoagent.mlp

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.momoagent.mlp.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity(), AgentWebSocketClient.Listener {
    private lateinit var binding: ActivityMainBinding

    private val prefs by lazy { getSharedPreferences("momo_agent_mvp", MODE_PRIVATE) }
    private val httpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(4, TimeUnit.SECONDS)
            .build()
    }
    private val audioPlayer = StreamingPcmPlayer()
    private val conversation = mutableListOf<ConversationEntry>()

    private var wsClient: AgentWebSocketClient? = null
    private var previewJob: Job? = null
    private var currentAssistantIndex: Int? = null
    private var isRunningTurn = false

    private val speechPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                launchSpeechRecognizer()
            } else {
                showToast("录音权限未开启")
            }
        }

    private val speechLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val data = result.data ?: return@registerForActivityResult
            val results = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS).orEmpty()
            val transcript = results.firstOrNull()?.trim().orEmpty()
            if (transcript.isBlank()) {
                showToast("没有识别到语音文本")
                return@registerForActivityResult
            }
            binding.messageInput.setText(transcript)
            sendMessage(transcript)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        restoreSettings()
        renderConversation()
        updateStatus("未连接")
        updatePreviewStatus("预览未启动")
        setConnected(false)

        binding.connectButton.setOnClickListener {
            connect()
        }
        binding.disconnectButton.setOnClickListener {
            disconnect()
        }
        binding.sendButton.setOnClickListener {
            sendMessage(binding.messageInput.text?.toString().orEmpty())
        }
        binding.speakButton.setOnClickListener {
            beginSpeechInput()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        disconnect()
        audioPlayer.close()
        httpClient.dispatcher.executorService.shutdown()
        httpClient.connectionPool.evictAll()
    }

    override fun onSocketOpened() {
        runOnUiThread {
            updateStatus("WebSocket 已连接，等待服务 ready")
            setConnected(true)
        }
    }

    override fun onSocketMessage(text: String) {
        runOnUiThread {
            handleSocketMessage(text)
        }
    }

    override fun onSocketClosed(reason: String) {
        runOnUiThread {
            isRunningTurn = false
            updateStatus("连接已关闭: $reason")
            setConnected(false)
        }
    }

    override fun onSocketFailure(message: String) {
        runOnUiThread {
            isRunningTurn = false
            updateStatus("连接失败: $message")
            setConnected(false)
        }
    }

    private fun connect() {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            showToast("请先填写控制器 IP 或域名")
            return
        }
        saveSettings()
        disconnectSocketOnly()
        wsClient = AgentWebSocketClient(
            url = buildWsUrl(host, binding.agentPortInput.text?.toString().orEmpty()),
            listener = this,
        ).also { it.connect() }
        startPreviewLoop(host, binding.previewPortInput.text?.toString().orEmpty())
        binding.connectButton.isEnabled = false
        binding.disconnectButton.isEnabled = true
        updateStatus("正在连接 $host")
    }

    private fun disconnect() {
        previewJob?.cancel()
        previewJob = null
        disconnectSocketOnly()
        audioPlayer.reset()
        isRunningTurn = false
        currentAssistantIndex = null
        setConnected(false)
        binding.previewImage.setImageDrawable(null)
        updateStatus("已断开")
        updatePreviewStatus("预览未启动")
    }

    private fun disconnectSocketOnly() {
        wsClient?.close()
        wsClient = null
    }

    private fun sendMessage(rawMessage: String) {
        val message = rawMessage.trim()
        if (message.isBlank()) {
            showToast("消息不能为空")
            return
        }
        val client = wsClient
        if (client == null) {
            showToast("请先连接")
            return
        }
        if (isRunningTurn) {
            showToast("当前正在处理上一轮，请稍等")
            return
        }
        val sent = client.sendAsk(message, binding.withTtsCheck.isChecked)
        if (!sent) {
            showToast("发送失败，WebSocket 还没准备好")
            return
        }
        binding.messageInput.setText("")
        appendConversation("你", message)
        currentAssistantIndex = appendConversation("Momo", "")
        isRunningTurn = true
        audioPlayer.reset()
        updateStatus("请求已发送，等待回复")
    }

    private fun beginSpeechInput() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            launchSpeechRecognizer()
        } else {
            speechPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun launchSpeechRecognizer() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
            putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.speech_prompt))
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
        }
        try {
            speechLauncher.launch(intent)
        } catch (_: ActivityNotFoundException) {
            showToast("当前设备没有可用的语音识别服务")
        }
    }

    private fun handleSocketMessage(text: String) {
        val payload = try {
            JSONObject(text)
        } catch (_: Exception) {
            return
        }
        when (payload.optString("type")) {
            "ready" -> updateStatus("服务已就绪")
            "status" -> updateStatus("状态已刷新")
            "turn_started" -> {
                isRunningTurn = true
                audioPlayer.reset()
                updateStatus("Momo 正在思考")
            }
            "agent_delta" -> {
                val reply = payload.optJSONObject("data")?.optString("reply").orEmpty()
                updateAssistant(reply)
                updateStatus("Momo 正在回复")
            }
            "agent_reply" -> {
                val reply = payload.optJSONObject("data")?.optString("reply").orEmpty()
                updateAssistant(reply)
            }
            "turn_done" -> {
                isRunningTurn = false
                updateStatus("本轮完成")
            }
            "audio_chunk" -> {
                val chunk = payload.optString("pcm16_base64")
                val sampleRate = payload.optInt("sample_rate", 0)
                if (chunk.isNotBlank() && sampleRate > 0) {
                    audioPlayer.playChunk(chunk, sampleRate)
                }
            }
            "tts_started" -> updateStatus("TTS 正在播放")
            "tts_unavailable" -> updateStatus("TTS 不可用，已保留文本回复")
            "error" -> {
                isRunningTurn = false
                val message = payload.optString("message").ifBlank { "未知错误" }
                appendConversation("系统", "错误: $message")
                updateStatus("发生错误: $message")
            }
            "pong" -> {
            }
        }
    }

    private fun startPreviewLoop(host: String, rawPort: String) {
        previewJob?.cancel()
        val previewUrl = buildPreviewUrl(host, rawPort)
        previewJob = lifecycleScope.launch(Dispatchers.IO) {
            while (isActive) {
                try {
                    val request = Request.Builder()
                        .url("$previewUrl&t=${System.currentTimeMillis()}")
                        .header("Cache-Control", "no-cache")
                        .build()
                    httpClient.newCall(request).execute().use { response ->
                        if (!response.isSuccessful) {
                            withContext(Dispatchers.Main) {
                                updatePreviewStatus("预览暂不可用 (${response.code})")
                            }
                        } else {
                            val bytes = response.body?.bytes()
                            val bitmap = if (bytes == null || bytes.isEmpty()) {
                                null
                            } else {
                                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                            }
                            if (bitmap != null) {
                                withContext(Dispatchers.Main) {
                                    binding.previewImage.setImageBitmap(bitmap)
                                    updatePreviewStatus(
                                        "预览在线，frame=${response.header("X-Frame-Id") ?: "?"}",
                                    )
                                }
                            }
                        }
                    }
                } catch (exc: Exception) {
                    withContext(Dispatchers.Main) {
                        updatePreviewStatus("预览连接失败: ${exc.message ?: "unknown"}")
                    }
                }
                delay(350)
            }
        }
    }

    private fun restoreSettings() {
        binding.hostInput.setText(prefs.getString("host", "192.168.1.20"))
        binding.agentPortInput.setText(prefs.getString("agent_port", "8010"))
        binding.previewPortInput.setText(prefs.getString("preview_port", "8000"))
        binding.withTtsCheck.isChecked = prefs.getBoolean("with_tts", true)
    }

    private fun saveSettings() {
        prefs.edit()
            .putString("host", normalizedHost(binding.hostInput.text?.toString().orEmpty()))
            .putString("agent_port", binding.agentPortInput.text?.toString().orEmpty().ifBlank { "8010" })
            .putString("preview_port", binding.previewPortInput.text?.toString().orEmpty().ifBlank { "8000" })
            .putBoolean("with_tts", binding.withTtsCheck.isChecked)
            .apply()
    }

    private fun setConnected(connected: Boolean) {
        binding.connectButton.isEnabled = !connected
        binding.disconnectButton.isEnabled = connected
        binding.sendButton.isEnabled = connected
        binding.speakButton.isEnabled = connected
        binding.messageInput.isEnabled = connected
    }

    private fun appendConversation(role: String, text: String): Int {
        conversation += ConversationEntry(role = role, text = text)
        renderConversation()
        return conversation.lastIndex
    }

    private fun updateAssistant(text: String) {
        val index = currentAssistantIndex
        if (index == null || index !in conversation.indices) {
            currentAssistantIndex = appendConversation("Momo", text)
            return
        }
        conversation[index] = conversation[index].copy(text = text)
        renderConversation()
    }

    private fun renderConversation() {
        val content = if (conversation.isEmpty()) {
            getString(R.string.conversation_placeholder)
        } else {
            conversation.joinToString(separator = "\n\n") { entry ->
                "${entry.role}:\n${entry.text.ifBlank { "..." }}"
            }
        }
        binding.conversationText.text = content
        binding.mainScroll.post {
            binding.mainScroll.fullScroll(View.FOCUS_DOWN)
        }
    }

    private fun updateStatus(message: String) {
        binding.statusText.text = message
    }

    private fun updatePreviewStatus(message: String) {
        binding.previewStatusText.text = message
    }

    private fun showToast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    private fun normalizedHost(raw: String): String {
        return raw.trim()
            .removePrefix("http://")
            .removePrefix("https://")
            .removePrefix("ws://")
            .removePrefix("wss://")
            .trimEnd('/')
    }

    private fun buildWsUrl(host: String, rawPort: String): String {
        val port = rawPort.trim().ifBlank { "8010" }
        return "ws://$host:$port/api/v1/ws/agent-stream"
    }

    private fun buildPreviewUrl(host: String, rawPort: String): String {
        val port = rawPort.trim().ifBlank { "8000" }
        return "http://$host:$port/frame.jpg?max_width=960&quality=70"
    }

    private data class ConversationEntry(
        val role: String,
        val text: String,
    )
}
