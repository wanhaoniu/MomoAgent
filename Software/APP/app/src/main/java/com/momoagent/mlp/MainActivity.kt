package com.momoagent.mlp

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.View
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
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
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity(), AgentWebSocketClient.Listener {
    private lateinit var binding: ActivityMainBinding

    private val prefs by lazy { getSharedPreferences("momo_agent_mvp", MODE_PRIVATE) }
    private val httpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .build()
    }
    private val audioPlayer = StreamingPcmPlayer()
    private val conversation = mutableListOf<ConversationEntry>()

    private var wsClient: AgentWebSocketClient? = null
    private var previewJob: Job? = null
    private var currentAssistantIndex: Int? = null
    private var isRunningTurn = false

    private var haiguitangModeVisible = false
    private var haiguitangIntroTimeoutJob: Job? = null
    private var haiguitangSceneConfig = HaiGuiTangSceneConfig()
    private var shouldResumePreviewAfterHaiGuiTang = false
    private var haiguitangControlPrimed = false

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
        initializeHaiGuiTangUi()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (haiguitangModeVisible) {
                    closeHaiGuiTangMode()
                    return
                }
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
            }
        })

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
        binding.haiguitangEntryButton.setOnClickListener {
            openHaiGuiTangMode()
        }
        binding.haiguitangCloseButton.setOnClickListener {
            closeHaiGuiTangMode()
        }
        binding.haiguitangSkipButton.setOnClickListener {
            showHaiGuiTangInteractive()
        }
        binding.haiguitangEnterButton.setOnClickListener {
            showHaiGuiTangInteractive()
        }
        binding.haiguitangReplayIntroButton.setOnClickListener {
            replayHaiGuiTangIntro()
        }
        binding.haiguitangNodButton.setOnClickListener {
            triggerHaiGuiTangAction(
                action = "nod",
                inFlightText = "Momo 点头中...",
                doneText = "Momo 刚刚点了点头。",
            )
        }
        binding.haiguitangShakeButton.setOnClickListener {
            triggerHaiGuiTangAction(
                action = "shake",
                inFlightText = "Momo 摇头中...",
                doneText = "Momo 刚刚摇了摇头。",
            )
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
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
        if (!haiguitangModeVisible) {
            startPreviewLoop(host, binding.previewPortInput.text?.toString().orEmpty())
        }
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
        haiguitangControlPrimed = false
        setConnected(false)
        binding.previewImage.setImageDrawable(null)
        updateStatus("已断开")
        updatePreviewStatus("预览未启动")
        if (haiguitangModeVisible) {
            updateHaiGuiTangStatus("连接已断开，可继续查看占位画面。")
        }
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

    private fun initializeHaiGuiTangUi() {
        binding.haiguitangOverlay.visibility = View.GONE
        binding.haiguitangIntroContainer.visibility = View.VISIBLE
        binding.haiguitangInteractiveContainer.visibility = View.GONE
        binding.haiguitangVideoView.visibility = View.GONE
        binding.haiguitangPlaceholderContainer.visibility = View.VISIBLE
        binding.haiguitangTitleText.text = getString(R.string.haiguitang_title)
        binding.haiguitangSubtitleText.text = getString(R.string.haiguitang_subtitle_default)
        binding.haiguitangStatusText.text = getString(R.string.haiguitang_status_default)
        binding.haiguitangVideoStatusText.text = getString(R.string.haiguitang_video_waiting)
    }

    private fun openHaiGuiTangMode() {
        if (haiguitangModeVisible) {
            return
        }

        saveSettings()
        haiguitangModeVisible = true
        haiguitangControlPrimed = false
        shouldResumePreviewAfterHaiGuiTang = previewJob?.isActive == true
        previewJob?.cancel()
        previewJob = null
        audioPlayer.reset()

        binding.haiguitangOverlay.visibility = View.VISIBLE
        binding.haiguitangOverlay.bringToFront()
        binding.haiguitangTitleText.text = getString(R.string.haiguitang_title)
        binding.haiguitangSubtitleText.text = getString(R.string.haiguitang_subtitle_default)
        binding.haiguitangExpressionPlaceholderText.text =
            getString(R.string.haiguitang_expression_placeholder)

        showHaiGuiTangPlaceholder(
            title = getString(R.string.haiguitang_placeholder_title),
            body = getString(R.string.haiguitang_status_loading),
            status = getString(R.string.haiguitang_video_waiting),
        )
        updateHaiGuiTangStatus(getString(R.string.haiguitang_status_loading))

        lifecycleScope.launch {
            val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
            val agentPort = binding.agentPortInput.text?.toString().orEmpty()
            val configResult = if (host.isBlank()) {
                ApiCallResult(
                    ok = false,
                    body = null,
                    message = "还没有填写控制器地址，当前先显示占位场景。",
                )
            } else {
                executeJsonRequest(
                    Request.Builder()
                        .url(buildHaiGuiTangSceneConfigUrl(host, agentPort))
                        .header("Cache-Control", "no-cache")
                        .build(),
                )
            }
            if (!haiguitangModeVisible) {
                return@launch
            }
            haiguitangSceneConfig = if (configResult.ok) {
                HaiGuiTangSceneConfig.fromApiEnvelope(configResult.body)
            } else {
                HaiGuiTangSceneConfig()
            }
            applyHaiGuiTangSceneConfig(configResult)
        }
    }

    private fun closeHaiGuiTangMode() {
        if (!haiguitangModeVisible) {
            return
        }

        haiguitangModeVisible = false
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        binding.haiguitangOverlay.visibility = View.GONE

        val shouldStopControl = haiguitangControlPrimed
        haiguitangControlPrimed = false
        if (shouldStopControl) {
            lifecycleScope.launch {
                stopHaiGuiTangControlQuietly()
            }
        }

        if (shouldResumePreviewAfterHaiGuiTang) {
            val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
            if (host.isNotBlank()) {
                startPreviewLoop(host, binding.previewPortInput.text?.toString().orEmpty())
            }
        }
    }

    private fun applyHaiGuiTangSceneConfig(configResult: ApiCallResult) {
        val config = haiguitangSceneConfig
        binding.haiguitangTitleText.text = config.title
        binding.haiguitangSubtitleText.text = config.subtitle
        binding.haiguitangStatusText.text = config.defaultStatusText
        binding.haiguitangSkipButton.visibility =
            if (config.introVideoSkipable) View.VISIBLE else View.GONE

        val placeholderBody = buildHaiGuiTangPlaceholderBody(
            config = config,
            extraMessage = configResult.message.takeIf { it.isNotBlank() },
        )
        val introVideoUrl = config.resolvedIntroVideoUrl(
            buildApiBaseUrl(
                normalizedHost(binding.hostInput.text?.toString().orEmpty()),
                binding.agentPortInput.text?.toString().orEmpty(),
            ),
        )

        if (introVideoUrl.isBlank()) {
            showHaiGuiTangPlaceholder(
                title = config.placeholderTitle,
                body = placeholderBody,
                status = "当前没有配置片头视频，已显示占位画面。",
            )
            return
        }

        playHaiGuiTangIntro(introVideoUrl, placeholderBody)
    }

    private fun buildHaiGuiTangPlaceholderBody(
        config: HaiGuiTangSceneConfig,
        extraMessage: String? = null,
    ): String {
        val sections = mutableListOf<String>()
        extraMessage?.trim()?.takeIf { it.isNotBlank() }?.let { sections += it }
        config.placeholderBody.trim().takeIf { it.isNotBlank() }?.let { sections += it }
        if (config.mediaFilePath.isNotBlank()) {
            sections += "后续把视频放到:\n${config.mediaFilePath}"
        }
        return sections.joinToString(separator = "\n\n")
    }

    private fun showHaiGuiTangPlaceholder(
        title: String,
        body: String,
        status: String,
    ) {
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        binding.haiguitangIntroContainer.visibility = View.VISIBLE
        binding.haiguitangInteractiveContainer.visibility = View.GONE
        binding.haiguitangVideoView.visibility = View.GONE
        binding.haiguitangPlaceholderContainer.visibility = View.VISIBLE
        binding.haiguitangPlaceholderTitleText.text = title
        binding.haiguitangPlaceholderBodyText.text = body
        binding.haiguitangVideoStatusText.text = status
        binding.haiguitangEnterButton.visibility = View.VISIBLE
    }

    private fun playHaiGuiTangIntro(
        introVideoUrl: String,
        placeholderBody: String,
    ) {
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        binding.haiguitangIntroContainer.visibility = View.VISIBLE
        binding.haiguitangInteractiveContainer.visibility = View.GONE
        binding.haiguitangVideoView.visibility = View.VISIBLE
        binding.haiguitangPlaceholderContainer.visibility = View.GONE
        binding.haiguitangVideoStatusText.text = "片头加载中..."

        binding.haiguitangVideoView.setOnPreparedListener {
            binding.haiguitangVideoStatusText.text = "片头播放中..."
            if (haiguitangSceneConfig.introVideoAutoPlay) {
                binding.haiguitangVideoView.start()
            }
        }
        binding.haiguitangVideoView.setOnCompletionListener {
            binding.haiguitangVideoStatusText.text = "片头播放结束，正在进入互动区。"
            showHaiGuiTangInteractive()
        }
        binding.haiguitangVideoView.setOnErrorListener { _, _, _ ->
            showHaiGuiTangPlaceholder(
                title = haiguitangSceneConfig.placeholderTitle,
                body = placeholderBody,
                status = "片头加载失败，已切换到占位画面。",
            )
            true
        }
        startHaiGuiTangIntroTimeout(haiguitangSceneConfig.introVideoTimeoutSec)
        binding.haiguitangVideoView.setVideoURI(Uri.parse(introVideoUrl))
    }

    private fun replayHaiGuiTangIntro() {
        if (!haiguitangModeVisible) {
            return
        }
        val introVideoUrl = haiguitangSceneConfig.resolvedIntroVideoUrl(
            buildApiBaseUrl(
                normalizedHost(binding.hostInput.text?.toString().orEmpty()),
                binding.agentPortInput.text?.toString().orEmpty(),
            ),
        )
        val placeholderBody = buildHaiGuiTangPlaceholderBody(config = haiguitangSceneConfig)
        if (introVideoUrl.isBlank()) {
            showHaiGuiTangPlaceholder(
                title = haiguitangSceneConfig.placeholderTitle,
                body = placeholderBody,
                status = "当前没有片头视频，显示占位画面。",
            )
            return
        }
        playHaiGuiTangIntro(introVideoUrl, placeholderBody)
    }

    private fun showHaiGuiTangInteractive() {
        if (!haiguitangModeVisible) {
            return
        }
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        binding.haiguitangIntroContainer.visibility = View.GONE
        binding.haiguitangInteractiveContainer.visibility = View.VISIBLE
        binding.haiguitangStatusText.text = haiguitangSceneConfig.defaultStatusText
        setHaiGuiTangActionButtonsEnabled(true)
    }

    private fun triggerHaiGuiTangAction(
        action: String,
        inFlightText: String,
        doneText: String,
    ) {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            updateHaiGuiTangStatus("请先填写控制器地址，当前只能查看占位页面。")
            showToast("请先填写控制器 IP 或域名")
            return
        }

        val agentPort = binding.agentPortInput.text?.toString().orEmpty()
        setHaiGuiTangActionButtonsEnabled(false)
        updateHaiGuiTangStatus(inFlightText)
        lifecycleScope.launch {
            val ready = ensureHaiGuiTangControlStarted(host, agentPort)
            if (!ready) {
                setHaiGuiTangActionButtonsEnabled(true)
                return@launch
            }
            val result = executeJsonRequest(
                request = Request.Builder()
                    .url(buildHaiGuiTangActionUrl(host, agentPort))
                    .post(
                        JSONObject()
                            .put("action", action)
                            .toString()
                            .toRequestBody("application/json; charset=utf-8".toMediaType()),
                    )
                    .build(),
            )
            if (result.ok) {
                updateHaiGuiTangStatus(doneText)
            } else {
                updateHaiGuiTangStatus("动作触发失败: ${result.message}")
                showToast(result.message)
                haiguitangControlPrimed = false
            }
            setHaiGuiTangActionButtonsEnabled(true)
        }
    }

    private suspend fun ensureHaiGuiTangControlStarted(host: String, rawPort: String): Boolean {
        if (haiguitangControlPrimed) {
            return true
        }
        val result = executeJsonRequest(
            request = Request.Builder()
                .url(buildHaiGuiTangStartUrl(host, rawPort))
                .post(
                    JSONObject()
                        .put("pan_joint", "shoulder_pan")
                        .put("tilt_joint", "elbow_flex")
                        .put("speed_percent", 30)
                        .put("nod_amplitude_deg", 7.0)
                        .put("nod_cycles", 2)
                        .put("shake_amplitude_deg", 10.0)
                        .put("shake_cycles", 2)
                        .put("beat_duration_sec", 0.26)
                        .put("beat_pause_sec", 0.08)
                        .put("return_duration_sec", 0.24)
                        .put("settle_pause_sec", 0.10)
                        .put("auto_center_after_action", true)
                        .put("capture_anchor_on_start", true)
                        .toString()
                        .toRequestBody("application/json; charset=utf-8".toMediaType()),
                )
                .build(),
        )
        if (!result.ok) {
            updateHaiGuiTangStatus("动作服务未就绪: ${result.message}")
            showToast(result.message)
            haiguitangControlPrimed = false
            return false
        }
        haiguitangControlPrimed = true
        return true
    }

    private suspend fun stopHaiGuiTangControlQuietly() {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            return
        }
        executeJsonRequest(
            request = Request.Builder()
                .url(buildHaiGuiTangStopUrl(host, binding.agentPortInput.text?.toString().orEmpty()))
                .post("{}".toRequestBody("application/json; charset=utf-8".toMediaType()))
                .build(),
        )
    }

    private fun setHaiGuiTangActionButtonsEnabled(enabled: Boolean) {
        binding.haiguitangNodButton.isEnabled = enabled
        binding.haiguitangShakeButton.isEnabled = enabled
    }

    private fun updateHaiGuiTangStatus(message: String) {
        binding.haiguitangStatusText.text = message
    }

    private fun startHaiGuiTangIntroTimeout(timeoutSec: Double) {
        cancelHaiGuiTangIntroTimeout()
        if (timeoutSec <= 0.0) {
            return
        }
        haiguitangIntroTimeoutJob = lifecycleScope.launch {
            delay((timeoutSec * 1000.0).toLong())
            if (haiguitangModeVisible && binding.haiguitangInteractiveContainer.visibility != View.VISIBLE) {
                showHaiGuiTangInteractive()
            }
        }
    }

    private fun cancelHaiGuiTangIntroTimeout() {
        haiguitangIntroTimeoutJob?.cancel()
        haiguitangIntroTimeoutJob = null
    }

    private fun stopHaiGuiTangVideo() {
        try {
            binding.haiguitangVideoView.stopPlayback()
        } catch (_: Exception) {
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

    private suspend fun executeJsonRequest(request: Request): ApiCallResult {
        return withContext(Dispatchers.IO) {
            try {
                httpClient.newCall(request).execute().use { response ->
                    val bodyText = response.body?.string().orEmpty()
                    val bodyJson = try {
                        if (bodyText.isBlank()) null else JSONObject(bodyText)
                    } catch (_: Exception) {
                        null
                    }
                    val ok = response.isSuccessful && bodyJson?.optBoolean("ok") == true
                    val message = when {
                        ok -> ""
                        bodyJson != null -> {
                            bodyJson.optJSONObject("error")?.optString("message")
                                ?.takeIf { it.isNotBlank() }
                                ?: bodyJson.optString("message").takeIf { it.isNotBlank() }
                                ?: "HTTP ${response.code}"
                        }
                        bodyText.isNotBlank() -> bodyText
                        else -> "HTTP ${response.code}"
                    }
                    ApiCallResult(ok = ok, body = bodyJson, message = message)
                }
            } catch (exc: Exception) {
                ApiCallResult(
                    ok = false,
                    body = null,
                    message = exc.message ?: "网络请求失败",
                )
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

    private fun buildApiBaseUrl(host: String, rawPort: String): String {
        val port = rawPort.trim().ifBlank { "8010" }
        return "http://$host:$port/"
    }

    private fun buildHaiGuiTangSceneConfigUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/scenes/haiguitang/config"
    }

    private fun buildHaiGuiTangStartUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/haiguitang/start"
    }

    private fun buildHaiGuiTangActionUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/haiguitang/act"
    }

    private fun buildHaiGuiTangStopUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/haiguitang/stop"
    }

    private data class ConversationEntry(
        val role: String,
        val text: String,
    )

    private data class ApiCallResult(
        val ok: Boolean,
        val body: JSONObject?,
        val message: String,
    )
}
