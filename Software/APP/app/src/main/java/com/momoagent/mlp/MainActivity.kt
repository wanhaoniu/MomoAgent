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
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
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
    private var isSocketOpen = false

    private var haiguitangModeVisible = false
    private var haiguitangIntroTimeoutJob: Job? = null
    private var haiguitangScenePollJob: Job? = null
    private var haiguitangSceneConfig = HaiGuiTangSceneConfig()
    private var shouldResumePreviewAfterHaiGuiTang = false
    private var haiguitangControlPrimed = false
    private var haiguitangLastAppliedSceneVersion = Int.MIN_VALUE

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
            if (haiguitangModeVisible) {
                updateHaiGuiTangFloatingSubtitle(transcript)
            }
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

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && haiguitangModeVisible) {
            setHaiGuiTangFullscreen(true)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        stopHaiGuiTangScenePolling()
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        stopHaiGuiTangExpressionVideo()
        setHaiGuiTangFullscreen(false)
        disconnect()
        audioPlayer.close()
        httpClient.dispatcher.executorService.shutdown()
        httpClient.connectionPool.evictAll()
    }

    override fun onSocketOpened() {
        isSocketOpen = true
        runOnUiThread {
            updateStatus("WebSocket 已连接，等待服务 ready")
            setConnected(true)
            if (haiguitangModeVisible && !isRunningTurn) {
                updateHaiGuiTangStatus("已连接 agent，等待场景指令或手动调试。")
            }
        }
    }

    override fun onSocketMessage(text: String) {
        runOnUiThread {
            handleSocketMessage(text)
        }
    }

    override fun onSocketClosed(reason: String) {
        isSocketOpen = false
        runOnUiThread {
            isRunningTurn = false
            updateStatus("连接已关闭: $reason")
            setConnected(false)
            if (haiguitangModeVisible) {
                updateHaiGuiTangStatus("Agent 连接已关闭，场景仍可继续播放。")
            }
        }
    }

    override fun onSocketFailure(message: String) {
        isSocketOpen = false
        runOnUiThread {
            isRunningTurn = false
            updateStatus("连接失败: $message")
            setConnected(false)
            if (haiguitangModeVisible) {
                updateHaiGuiTangStatus("Agent 连接失败，场景切换会退回到本地调试模式。")
            }
        }
    }

    private fun connect() {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            showToast("请先填写控制器 IP 或域名")
            return
        }
        saveSettings()
        isSocketOpen = false
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
        if (haiguitangModeVisible) {
            updateHaiGuiTangStatus("正在连接 agent...")
        }
    }

    private fun disconnect() {
        previewJob?.cancel()
        previewJob = null
        disconnectSocketOnly()
        audioPlayer.reset()
        isRunningTurn = false
        isSocketOpen = false
        currentAssistantIndex = null
        haiguitangControlPrimed = false
        setConnected(false)
        binding.previewImage.setImageDrawable(null)
        updateStatus("已断开")
        updatePreviewStatus("预览未启动")
        if (haiguitangModeVisible) {
            updateHaiGuiTangStatus("连接已断开，可继续查看场景或手动触发动作。")
        }
    }

    private fun disconnectSocketOnly() {
        wsClient?.close()
        wsClient = null
        isSocketOpen = false
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
        if (!isSocketOpen) {
            showToast("Agent 还在连接中，请稍等")
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
        if (haiguitangModeVisible) {
            updateHaiGuiTangStatus("你：$message")
        }
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
            "ready" -> {
                updateStatus("服务已就绪")
                if (haiguitangModeVisible && !isRunningTurn) {
                    updateHaiGuiTangStatus("已连接 agent，等待场景指令或手动调试。")
                }
            }
            "status" -> updateStatus("状态已刷新")
            "turn_started" -> {
                isRunningTurn = true
                audioPlayer.reset()
                updateStatus("Momo 正在思考")
                if (haiguitangModeVisible) {
                    updateHaiGuiTangStatus("Momo 正在思考...")
                }
            }
            "agent_delta" -> {
                val reply = payload.optJSONObject("data")?.optString("reply").orEmpty()
                updateAssistant(reply)
                updateStatus("Momo 正在回复")
                if (haiguitangModeVisible) {
                    updateHaiGuiTangStatus("Momo 正在回复...")
                }
            }
            "agent_reply" -> {
                val reply = payload.optJSONObject("data")?.optString("reply").orEmpty()
                updateAssistant(reply)
            }
            "turn_done" -> {
                isRunningTurn = false
                updateStatus("本轮完成")
                if (haiguitangModeVisible) {
                    updateHaiGuiTangStatus("本轮结束，等待下一次场景切换。")
                }
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
                if (haiguitangModeVisible) {
                    updateHaiGuiTangStatus("发生错误: $message")
                }
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
        binding.haiguitangExpressionVideoView.visibility = View.GONE
        binding.haiguitangTitleText.text = getString(R.string.haiguitang_title)
        binding.haiguitangSubtitleText.text = getString(R.string.haiguitang_subtitle_default)
        updateHaiGuiTangStatus(getString(R.string.haiguitang_status_default))
        binding.haiguitangVideoStatusText.text = getString(R.string.haiguitang_video_waiting)
        updateHaiGuiTangFloatingSubtitle("")
        showHaiGuiTangExpressionPlaceholder(getString(R.string.haiguitang_expression_placeholder))
    }

    private fun openHaiGuiTangMode() {
        if (haiguitangModeVisible) {
            return
        }

        saveSettings()
        haiguitangModeVisible = true
        haiguitangControlPrimed = false
        haiguitangLastAppliedSceneVersion = Int.MIN_VALUE
        shouldResumePreviewAfterHaiGuiTang = previewJob?.isActive == true
        previewJob?.cancel()
        previewJob = null
        audioPlayer.reset()
        stopHaiGuiTangScenePolling()

        setHaiGuiTangFullscreen(true)
        binding.haiguitangOverlay.visibility = View.VISIBLE
        binding.haiguitangOverlay.bringToFront()
        binding.haiguitangTitleText.text = getString(R.string.haiguitang_title)
        binding.haiguitangSubtitleText.text = getString(R.string.haiguitang_subtitle_default)

        showHaiGuiTangPlaceholder(
            title = getString(R.string.haiguitang_placeholder_title),
            body = getString(R.string.haiguitang_status_loading),
            status = getString(R.string.haiguitang_video_waiting),
        )
        updateHaiGuiTangFloatingSubtitle("")
        showHaiGuiTangExpressionPlaceholder(getString(R.string.haiguitang_expression_placeholder))
        updateHaiGuiTangStatus(getString(R.string.haiguitang_status_loading))

        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isNotBlank() && !isSocketOpen) {
            connect()
        }

        lifecycleScope.launch {
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
        haiguitangLastAppliedSceneVersion = Int.MIN_VALUE
        stopHaiGuiTangScenePolling()
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        stopHaiGuiTangExpressionVideo()
        updateHaiGuiTangFloatingSubtitle("")
        binding.haiguitangOverlay.visibility = View.GONE
        setHaiGuiTangFullscreen(false)

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
        updateHaiGuiTangStatus(config.defaultStatusText)
        binding.haiguitangSkipButton.visibility =
            if (config.introVideoSkipable) View.VISIBLE else View.GONE

        val placeholderBody = buildHaiGuiTangPlaceholderBody(
            config = config,
            extraMessage = configResult.message.takeIf { it.isNotBlank() },
        )
        val baseUrl = buildHaiGuiTangBaseUrl()
        val introVideoUrl = config.resolvedIntroVideoUrl(baseUrl)
        val defaultVideoUrl = config.resolvedDefaultVideoUrl(baseUrl)

        if (introVideoUrl.isBlank() && defaultVideoUrl.isNotBlank()) {
            showHaiGuiTangInteractive()
            return
        }

        if (introVideoUrl.isBlank()) {
            showHaiGuiTangPlaceholder(
                title = config.placeholderTitle,
                body = placeholderBody,
                status = "当前没有检测到 begin.mp4 或 default.mp4，已显示占位画面。",
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
        config.mediaDirectoryPath.trim().takeIf { it.isNotBlank() }?.let { sections += "素材目录:\n$it" }
        return sections.joinToString(separator = "\n\n")
    }

    private fun showHaiGuiTangPlaceholder(
        title: String,
        body: String,
        status: String,
    ) {
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        stopHaiGuiTangExpressionVideo()
        updateHaiGuiTangFloatingSubtitle("")
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
        stopHaiGuiTangExpressionVideo()
        binding.haiguitangIntroContainer.visibility = View.VISIBLE
        binding.haiguitangInteractiveContainer.visibility = View.GONE
        binding.haiguitangVideoView.visibility = View.VISIBLE
        binding.haiguitangPlaceholderContainer.visibility = View.GONE
        binding.haiguitangEnterButton.visibility = View.GONE
        binding.haiguitangVideoStatusText.text = "片头加载中..."
        updateHaiGuiTangStatus("片头播放中...")

        binding.haiguitangVideoView.setOnPreparedListener { mediaPlayer ->
            mediaPlayer.isLooping = false
            binding.haiguitangVideoStatusText.text = "片头播放中..."
            if (haiguitangSceneConfig.introVideoAutoPlay) {
                binding.haiguitangVideoView.start()
            } else {
                binding.haiguitangVideoStatusText.text = "片头已就绪，可手动进入互动态。"
                binding.haiguitangEnterButton.visibility = View.VISIBLE
            }
        }
        binding.haiguitangVideoView.setOnCompletionListener {
            binding.haiguitangVideoStatusText.text = "片头播放结束，正在进入角色场景。"
            enterHaiGuiTangInteractiveWithDefaultClip()
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
        updateHaiGuiTangFloatingSubtitle("")
        val introVideoUrl = haiguitangSceneConfig.resolvedIntroVideoUrl(buildHaiGuiTangBaseUrl())
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
        enterHaiGuiTangInteractiveWithDefaultClip()
    }

    private fun showHaiGuiTangInteractiveShell() {
        if (!haiguitangModeVisible) {
            return
        }
        cancelHaiGuiTangIntroTimeout()
        stopHaiGuiTangVideo()
        binding.haiguitangIntroContainer.visibility = View.GONE
        binding.haiguitangInteractiveContainer.visibility = View.VISIBLE
        ensureHaiGuiTangScenePolling()
        setHaiGuiTangActionButtonsEnabled(true)
    }

    private fun enterHaiGuiTangInteractiveWithDefaultClip() {
        showHaiGuiTangInteractiveShell()
        applyHaiGuiTangSceneState(
            sceneState = HaiGuiTangSceneState(
                version = if (haiguitangLastAppliedSceneVersion == Int.MIN_VALUE) 0 else haiguitangLastAppliedSceneVersion,
                clip = "default",
                subtitleText = "",
                videoUrl = haiguitangSceneConfig.resolvedDefaultVideoUrl(buildHaiGuiTangBaseUrl()),
                loopPlayback = true,
                defaultVideoUrl = haiguitangSceneConfig.resolvedDefaultVideoUrl(buildHaiGuiTangBaseUrl()),
            ),
            force = true,
        )
    }

    private fun ensureHaiGuiTangScenePolling() {
        if (!haiguitangModeVisible || haiguitangScenePollJob?.isActive == true) {
            return
        }
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            return
        }
        val agentPort = binding.agentPortInput.text?.toString().orEmpty()
        haiguitangScenePollJob = lifecycleScope.launch {
            while (isActive && haiguitangModeVisible) {
                val result = executeJsonRequest(
                    request = Request.Builder()
                        .url(buildHaiGuiTangSceneStateUrl(host, agentPort))
                        .header("Cache-Control", "no-cache")
                        .build(),
                )
                if (result.ok) {
                    applyHaiGuiTangSceneState(HaiGuiTangSceneState.fromApiEnvelope(result.body))
                }
                delay(700)
            }
        }
    }

    private fun stopHaiGuiTangScenePolling() {
        haiguitangScenePollJob?.cancel()
        haiguitangScenePollJob = null
    }

    private fun applyHaiGuiTangSceneState(
        sceneState: HaiGuiTangSceneState,
        force: Boolean = false,
    ) {
        if (!haiguitangModeVisible) {
            return
        }

        if (!force && sceneState.version == haiguitangLastAppliedSceneVersion) {
            return
        }
        haiguitangLastAppliedSceneVersion = sceneState.version
        updateHaiGuiTangFloatingSubtitle(sceneState.subtitleText)

        val baseUrl = buildHaiGuiTangBaseUrl()
        when (sceneState.normalizedClip()) {
            "intro" -> {
                val introVideoUrl = sceneState.resolvedVideoUrl(baseUrl)
                    .ifBlank { haiguitangSceneConfig.resolvedIntroVideoUrl(baseUrl) }
                if (introVideoUrl.isBlank()) {
                    enterHaiGuiTangInteractiveWithDefaultClip()
                    return
                }
                playHaiGuiTangIntro(
                    introVideoUrl = introVideoUrl,
                    placeholderBody = buildHaiGuiTangPlaceholderBody(config = haiguitangSceneConfig),
                )
            }

            "nod" -> {
                showHaiGuiTangInteractiveShell()
                updateHaiGuiTangStatus("已切换到点头反馈。")
                playHaiGuiTangExpressionVideo(
                    videoUrl = sceneState.resolvedVideoUrl(baseUrl)
                        .ifBlank { haiguitangSceneConfig.resolvedNodVideoUrl(baseUrl) },
                    loopPlayback = sceneState.loopPlayback,
                    placeholderText = "点头视频未就绪，已回到默认角色表情。",
                    fallbackToDefault = !sceneState.loopPlayback,
                )
            }

            "shake" -> {
                showHaiGuiTangInteractiveShell()
                updateHaiGuiTangStatus("已切换到摇头反馈。")
                playHaiGuiTangExpressionVideo(
                    videoUrl = sceneState.resolvedVideoUrl(baseUrl)
                        .ifBlank { haiguitangSceneConfig.resolvedShakeVideoUrl(baseUrl) },
                    loopPlayback = sceneState.loopPlayback,
                    placeholderText = "摇头视频未就绪，已回到默认角色表情。",
                    fallbackToDefault = !sceneState.loopPlayback,
                )
            }

            "outro" -> {
                showHaiGuiTangInteractiveShell()
                updateHaiGuiTangStatus("已切换到结束片段。")
                playHaiGuiTangExpressionVideo(
                    videoUrl = sceneState.resolvedVideoUrl(baseUrl)
                        .ifBlank { haiguitangSceneConfig.resolvedOutroVideoUrl(baseUrl) },
                    loopPlayback = sceneState.loopPlayback,
                    placeholderText = "结束片段未就绪，已回到默认角色表情。",
                    fallbackToDefault = !sceneState.loopPlayback,
                )
            }

            else -> {
                showHaiGuiTangInteractiveShell()
                val defaultVideoUrl = sceneState.resolvedVideoUrl(baseUrl)
                    .ifBlank { sceneState.resolvedDefaultVideoUrl(baseUrl) }
                    .ifBlank { haiguitangSceneConfig.resolvedDefaultVideoUrl(baseUrl) }
                updateHaiGuiTangStatus(haiguitangSceneConfig.defaultStatusText)
                playHaiGuiTangExpressionVideo(
                    videoUrl = defaultVideoUrl,
                    loopPlayback = sceneState.loopPlayback,
                    placeholderText = buildHaiGuiTangExpressionPlaceholder(),
                    fallbackToDefault = false,
                )
            }
        }
    }

    private fun triggerHaiGuiTangAction(
        action: String,
        inFlightText: String,
        doneText: String,
    ) {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            updateHaiGuiTangStatus("请先填写控制器地址，当前只能查看场景。")
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
                val sceneState = presentHaiGuiTangSceneState(
                    host = host,
                    rawPort = agentPort,
                    clip = action,
                    subtitleText = "",
                    loopPlayback = false,
                )
                applyHaiGuiTangSceneState(
                    sceneState = sceneState ?: buildManualHaiGuiTangSceneState(action),
                    force = true,
                )
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

    private suspend fun presentHaiGuiTangSceneState(
        host: String,
        rawPort: String,
        clip: String,
        subtitleText: String,
        loopPlayback: Boolean?,
    ): HaiGuiTangSceneState? {
        val payload = JSONObject()
            .put("clip", clip)
            .put("subtitle_text", subtitleText)
        if (loopPlayback != null) {
            payload.put("loop_playback", loopPlayback)
        }
        val result = executeJsonRequest(
            request = Request.Builder()
                .url(buildHaiGuiTangSceneStateUrl(host, rawPort))
                .post(
                    payload.toString()
                        .toRequestBody("application/json; charset=utf-8".toMediaType()),
                )
                .build(),
        )
        if (!result.ok) {
            return null
        }
        return HaiGuiTangSceneState.fromApiEnvelope(result.body)
    }

    private fun buildManualHaiGuiTangSceneState(action: String): HaiGuiTangSceneState {
        val baseUrl = buildHaiGuiTangBaseUrl()
        val videoUrl = when (action) {
            "nod" -> haiguitangSceneConfig.resolvedNodVideoUrl(baseUrl)
            "shake" -> haiguitangSceneConfig.resolvedShakeVideoUrl(baseUrl)
            else -> haiguitangSceneConfig.resolvedDefaultVideoUrl(baseUrl)
        }
        return HaiGuiTangSceneState(
            version = if (haiguitangLastAppliedSceneVersion == Int.MIN_VALUE) 0 else haiguitangLastAppliedSceneVersion,
            clip = action,
            subtitleText = "",
            videoUrl = videoUrl,
            loopPlayback = false,
            defaultVideoUrl = haiguitangSceneConfig.resolvedDefaultVideoUrl(baseUrl),
        )
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
        binding.haiguitangReplayIntroButton.isEnabled = enabled
    }

    private fun updateHaiGuiTangStatus(message: String) {
        binding.haiguitangStatusText.text = message
    }

    private fun updateHaiGuiTangFloatingSubtitle(message: String) {
        val normalized = message.trim()
        binding.haiguitangFloatingSubtitleText.text = normalized
        binding.haiguitangFloatingSubtitleText.visibility =
            if (normalized.isBlank()) View.GONE else View.VISIBLE
    }

    private fun buildHaiGuiTangExpressionPlaceholder(): String {
        return when {
            haiguitangSceneConfig.defaultVideoUrl.isNotBlank() ->
                "默认角色视频已就绪，等待 agent 通过 POST 切换表情。"

            haiguitangSceneConfig.mediaDirectoryPath.isNotBlank() ->
                "当前没有检测到 default.mp4。\n素材目录：\n${haiguitangSceneConfig.mediaDirectoryPath}"

            else -> getString(R.string.haiguitang_expression_placeholder)
        }
    }

    private fun showHaiGuiTangExpressionPlaceholder(message: String) {
        stopHaiGuiTangExpressionVideo()
        binding.haiguitangExpressionVideoView.visibility = View.GONE
        binding.haiguitangExpressionPlaceholderText.visibility = View.VISIBLE
        binding.haiguitangExpressionPlaceholderText.text = message
    }

    private fun playHaiGuiTangExpressionVideo(
        videoUrl: String,
        loopPlayback: Boolean,
        placeholderText: String,
        fallbackToDefault: Boolean,
    ) {
        if (videoUrl.isBlank()) {
            if (fallbackToDefault) {
                playHaiGuiTangDefaultClip()
            } else {
                showHaiGuiTangExpressionPlaceholder(placeholderText)
            }
            return
        }

        stopHaiGuiTangExpressionVideo()
        binding.haiguitangExpressionPlaceholderText.visibility = View.GONE
        binding.haiguitangExpressionVideoView.visibility = View.VISIBLE
        binding.haiguitangExpressionVideoView.setOnPreparedListener { mediaPlayer ->
            mediaPlayer.isLooping = loopPlayback
            binding.haiguitangExpressionVideoView.start()
        }
        binding.haiguitangExpressionVideoView.setOnCompletionListener {
            if (fallbackToDefault && !loopPlayback) {
                playHaiGuiTangDefaultClip()
            } else {
                showHaiGuiTangExpressionPlaceholder(placeholderText)
            }
        }
        binding.haiguitangExpressionVideoView.setOnErrorListener { _, _, _ ->
            if (fallbackToDefault) {
                playHaiGuiTangDefaultClip()
            } else {
                showHaiGuiTangExpressionPlaceholder(placeholderText)
            }
            true
        }
        binding.haiguitangExpressionVideoView.setVideoURI(Uri.parse(videoUrl))
    }

    private fun playHaiGuiTangDefaultClip() {
        val baseUrl = buildHaiGuiTangBaseUrl()
        val defaultVideoUrl = haiguitangSceneConfig.resolvedDefaultVideoUrl(baseUrl)
        updateHaiGuiTangStatus(haiguitangSceneConfig.defaultStatusText)
        if (defaultVideoUrl.isBlank()) {
            showHaiGuiTangExpressionPlaceholder(buildHaiGuiTangExpressionPlaceholder())
            return
        }
        playHaiGuiTangExpressionVideo(
            videoUrl = defaultVideoUrl,
            loopPlayback = true,
            placeholderText = buildHaiGuiTangExpressionPlaceholder(),
            fallbackToDefault = false,
        )
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

    private fun stopHaiGuiTangExpressionVideo() {
        try {
            binding.haiguitangExpressionVideoView.stopPlayback()
        } catch (_: Exception) {
        }
    }

    private fun setHaiGuiTangFullscreen(enabled: Boolean) {
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        if (enabled) {
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            controller.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
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

    private fun buildHaiGuiTangBaseUrl(): String {
        val host = normalizedHost(binding.hostInput.text?.toString().orEmpty())
        if (host.isBlank()) {
            return ""
        }
        return buildApiBaseUrl(host, binding.agentPortInput.text?.toString().orEmpty())
    }

    private fun buildHaiGuiTangSceneConfigUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/scenes/haiguitang/config"
    }

    private fun buildHaiGuiTangSceneStateUrl(host: String, rawPort: String): String {
        return buildApiBaseUrl(host, rawPort) + "api/v1/scenes/haiguitang/state"
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
