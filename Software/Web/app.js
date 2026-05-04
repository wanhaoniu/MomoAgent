(function () {
  const POLL_INTERVAL_MS = 800;
  const REQUEST_TIMEOUT_MS = 5000;
  const STT_TARGET_SAMPLE_RATE = 16000;
  const STT_CHUNK_DURATION_MS = 100;
  const STT_CHUNK_SAMPLES = Math.round((STT_TARGET_SAMPLE_RATE * STT_CHUNK_DURATION_MS) / 1000);
  const AGENT_REQUEST_TIMEOUT_MS = 60000;
  const VOICE_FINAL_DEBOUNCE_MS = 900;
  const VOICE_LISTEN_RESUME_DELAY_MS = 420;
  const TTS_PLAYBACK_TAIL_PAD_MS = 260;
  const VOICE_INTERRUPT_SETTLE_MS = 520;
  const MOTION_START_PAYLOAD = {
    pan_joint: "shoulder_pan",
    tilt_joint: "elbow_flex",
    speed_percent: 30,
    nod_amplitude_deg: 7.0,
    nod_cycles: 2,
    shake_amplitude_deg: 10.0,
    shake_cycles: 2,
    beat_duration_sec: 0.26,
    beat_pause_sec: 0.08,
    return_duration_sec: 0.24,
    settle_pause_sec: 0.1,
    auto_center_after_action: true,
    capture_anchor_on_start: true,
  };

  const elements = {
    sceneVideo: document.getElementById("sceneVideo"),
    sceneFallback: document.getElementById("sceneFallback"),
    fallbackTitle: document.getElementById("fallbackTitle"),
    fallbackBody: document.getElementById("fallbackBody"),
    sceneStatus: document.getElementById("sceneStatus"),
    clipBadge: document.getElementById("clipBadge"),
    syncBadge: document.getElementById("syncBadge"),
    subtitleText: document.getElementById("subtitleText"),
    riddleCard: document.getElementById("riddleCard"),
    riddleText: document.getElementById("riddleText"),
    riddleStatusBadge: document.getElementById("riddleStatusBadge"),
    turnCard: document.getElementById("turnCard"),
    turnUserText: document.getElementById("turnUserText"),
    turnAgentText: document.getElementById("turnAgentText"),
    difficultyButton: document.getElementById("difficultyButton"),
    voiceModeButton: document.getElementById("voiceModeButton"),
    voiceModeBadge: document.getElementById("voiceModeBadge"),
    userSpeechWrap: document.getElementById("userSpeechWrap"),
    userSpeechText: document.getElementById("userSpeechText"),
    modePicker: document.getElementById("modePicker"),
    modePickerHelpText: document.getElementById("modePickerHelpText"),
    apiBaseInput: document.getElementById("apiBaseInput"),
    apiHintText: document.getElementById("apiHintText"),
    reloadButton: document.getElementById("reloadButton"),
    saveApiButton: document.getElementById("saveApiButton"),
    agentPromptInput: document.getElementById("agentPromptInput"),
    agentAskButton: document.getElementById("agentAskButton"),
    sttStateBadge: document.getElementById("sttStateBadge"),
    sttMetaText: document.getElementById("sttMetaText"),
    audioInputSelect: document.getElementById("audioInputSelect"),
    audioInputRefreshButton: document.getElementById("audioInputRefreshButton"),
    audioInputHintText: document.getElementById("audioInputHintText"),
    sttStartButton: document.getElementById("sttStartButton"),
    sttStopButton: document.getElementById("sttStopButton"),
    sttClearButton: document.getElementById("sttClearButton"),
    sttUsePromptButton: document.getElementById("sttUsePromptButton"),
    sttFinalText: document.getElementById("sttFinalText"),
    sttPartialText: document.getElementById("sttPartialText"),
    subtitleInput: document.getElementById("subtitleInput"),
    sendSubtitleButton: document.getElementById("sendSubtitleButton"),
    clearSubtitleButton: document.getElementById("clearSubtitleButton"),
    hardwareNodButton: document.getElementById("hardwareNodButton"),
    hardwareShakeButton: document.getElementById("hardwareShakeButton"),
    logOutput: document.getElementById("logOutput"),
    clearLogButton: document.getElementById("clearLogButton"),
    consolePanel: document.getElementById("consolePanel"),
    consoleToggleButton: document.getElementById("consoleToggleButton"),
    consoleCloseButton: document.getElementById("consoleCloseButton"),
    fullscreenButton: document.getElementById("fullscreenButton"),
    modeButtons: Array.from(document.querySelectorAll(".mode-card")),
    sceneActionButtons: Array.from(document.querySelectorAll(".scene-action")),
  };

  const DIFFICULTY_OPTIONS = Object.freeze({
    easy: {
      label: "简单",
    },
    medium: {
      label: "中等",
    },
    hard: {
      label: "困难",
    },
  });

  const runtime = {
    apiBase: "",
    config: null,
    lastVersion: Number.MIN_SAFE_INTEGER,
    pollTimer: null,
    playToken: 0,
    motionPrimed: false,
    startupSceneHandled: false,
    latestSceneState: null,
    agentTurnPending: false,
    sttSocket: null,
    sttMediaStream: null,
    sttAudioContext: null,
    sttSourceNode: null,
    sttProcessorNode: null,
    sttPendingSamples: new Int16Array(0),
    sttFinalSegments: [],
    sttPartialText: "",
    sttCanSendAudio: false,
    sttStarting: false,
    sttSessionId: "",
    sttCurrentConfig: null,
    voiceModeActive: false,
    voicePhase: "idle",
    voiceDebounceTimer: null,
    voiceRestartTimer: null,
    voicePendingSegments: [],
    voiceLastUtterance: "",
    voiceTurnSerial: 0,
    ttsAudioContext: null,
    ttsSocket: null,
    ttsPlaybackCancel: null,
    ttsActiveSources: new Set(),
    ttsNextStartTime: 0,
    selectedDifficulty: "",
    modePickerOpen: false,
    modePickerStatusText: "进入后会由 Agent 先说题面。你听完后点击“开始录音”，就能开始和 Agent 对话。",
    roundStartPending: false,
    activeRiddleText: "",
    activeRiddleStatus: "idle",
    currentTurnUserText: "",
    currentTurnAgentText: "",
    audioInputDevices: [],
    selectedAudioInputId: "default",
  };

  function normalizeDifficulty(value) {
    const normalized = String(value || "").trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(DIFFICULTY_OPTIONS, normalized) ? normalized : "";
  }

  function difficultyLabel(value) {
    const normalized = normalizeDifficulty(value);
    return normalized ? DIFFICULTY_OPTIONS[normalized].label : "未选择";
  }

  function normalizeAudioInputId(value) {
    const raw = String(value || "").trim();
    return raw || "default";
  }

  function riddleStatusLabel(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (normalized === "ongoing") {
      return "进行中";
    }
    if (normalized === "solved") {
      return "已答对";
    }
    if (normalized === "revealed") {
      return "已揭晓";
    }
    return "等待开局";
  }

  function ttsCancellationReason(error) {
    return String((error && error.message) || error || "").trim().toLowerCase();
  }

  function isTtsCancellationError(error) {
    const message = ttsCancellationReason(error);
    return [
      "voice_mode_interrupt",
      "voice_mode_start",
      "voice_mode_disabled",
      "round_restart",
      "superseded",
      "tts playback cancelled",
      "tts 播放已取消",
    ].includes(message);
  }

  function safeStorageGet(key) {
    try {
      if (!window.localStorage || typeof window.localStorage.getItem !== "function") {
        return null;
      }
      return window.localStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  }

  function safeStorageSet(key, value) {
    try {
      if (!window.localStorage || typeof window.localStorage.setItem !== "function") {
        return;
      }
      window.localStorage.setItem(key, value);
    } catch (_error) {
    }
  }

  function delay(ms) {
    const waitMs = Math.max(0, Number(ms || 0));
    return new Promise((resolve) => {
      window.setTimeout(resolve, waitMs);
    });
  }

  function isLoopbackHost(hostname) {
    const host = String(hostname || "").trim().toLowerCase();
    return (
      host === "localhost" ||
      host === "::1" ||
      host === "[::1]" ||
      host === "0.0.0.0" ||
      /^127(?:\.\d{1,3}){3}$/.test(host)
    );
  }

  function currentOriginUrl() {
    const protocol = String(window.location.protocol || "").toLowerCase();
    const hostname = String(window.location.hostname || "").trim();
    if ((protocol !== "http:" && protocol !== "https:") || !hostname) {
      return null;
    }
    try {
      return new URL(window.location.href);
    } catch (_error) {
      return null;
    }
  }

  function defaultApiBase() {
    const currentUrl = currentOriginUrl();
    if (currentUrl) {
      return `${currentUrl.origin}/`;
    }
    return "http://127.0.0.1:8010/";
  }

  function normalizeCandidateUrl(rawValue) {
    const raw = String(rawValue || "").trim();
    if (!raw) {
      return null;
    }
    try {
      if (/^https?:\/\//i.test(raw)) {
        return new URL(raw);
      }
      if (/^[0-9a-z.-]+(?::[0-9]+)?$/i.test(raw)) {
        return new URL(`http://${raw}/`);
      }
    } catch (_error) {
      return null;
    }
    return null;
  }

  function maybeRewriteLoopbackToCurrentOrigin(targetUrl) {
    if (!targetUrl) {
      return null;
    }
    const currentUrl = currentOriginUrl();
    if (!currentUrl) {
      return targetUrl;
    }
    if (!isLoopbackHost(targetUrl.hostname) || isLoopbackHost(currentUrl.hostname)) {
      return targetUrl;
    }
    const rewritten = new URL(targetUrl.toString());
    rewritten.protocol = currentUrl.protocol;
    rewritten.hostname = currentUrl.hostname;
    if (currentUrl.port) {
      rewritten.port = currentUrl.port;
    }
    return rewritten;
  }

  function normalizeApiBase(rawValue) {
    const candidateUrl = maybeRewriteLoopbackToCurrentOrigin(normalizeCandidateUrl(rawValue));
    if (!candidateUrl) {
      return defaultApiBase();
    }
    return candidateUrl.toString().endsWith("/") ? candidateUrl.toString() : `${candidateUrl.toString()}/`;
  }

  function clearStoredApiBase() {
    try {
      if (!window.localStorage || typeof window.localStorage.removeItem !== "function") {
        return;
      }
      window.localStorage.removeItem("haiguitang_api_base");
    } catch (_error) {
    }
  }

  function readInitialApiBase() {
    const queryValue = new URLSearchParams(window.location.search).get("api");
    const storedValue = safeStorageGet("haiguitang_api_base");
    const normalized = normalizeApiBase(queryValue || storedValue || defaultApiBase());
    const normalizedUrl = normalizeCandidateUrl(normalized);
    const currentUrl = currentOriginUrl();

    if (
      normalizedUrl &&
      currentUrl &&
      isLoopbackHost(normalizedUrl.hostname) &&
      !isLoopbackHost(currentUrl.hostname)
    ) {
      clearStoredApiBase();
      return defaultApiBase();
    }

    return normalized;
  }

  function resolveApiUrl(path) {
    return new URL(path, runtime.apiBase || defaultApiBase()).toString();
  }

  function log(message) {
    const stamp = new Date().toLocaleTimeString("zh-CN", {
      hour12: false,
    });
    const nextLine = `[${stamp}] ${message}`;
    elements.logOutput.textContent = elements.logOutput.textContent === "Ready."
      ? nextLine
      : `${nextLine}\n${elements.logOutput.textContent}`;
  }

  function setStatus(message) {
    elements.sceneStatus.textContent = message;
  }

  function setClipBadge(clip) {
    elements.clipBadge.textContent = String(clip || "default");
  }

  function setSyncBadge(message, online) {
    elements.syncBadge.textContent = message;
    elements.syncBadge.classList.toggle("is-offline", !online);
  }

  function setAgentBusy(busy) {
    runtime.agentTurnPending = Boolean(busy);
    if (elements.agentAskButton) {
      elements.agentAskButton.disabled = Boolean(busy);
      elements.agentAskButton.textContent = busy ? "Agent 思考中..." : "问 Agent";
    }
  }

  function persistActiveRiddle() {
    safeStorageSet(
      "haiguitang_active_riddle",
      JSON.stringify({
        text: String(runtime.activeRiddleText || "").trim(),
        status: String(runtime.activeRiddleStatus || "idle").trim(),
      }),
    );
  }

  function clearPersistedActiveRiddle() {
    try {
      if (!window.localStorage || typeof window.localStorage.removeItem !== "function") {
        return;
      }
      window.localStorage.removeItem("haiguitang_active_riddle");
    } catch (_error) {
    }
  }

  function setActiveRiddle(text, status) {
    runtime.activeRiddleText = String(text || "").trim();
    runtime.activeRiddleStatus = String(status || "idle").trim().toLowerCase() || "idle";
    if (runtime.activeRiddleText) {
      persistActiveRiddle();
    } else {
      clearPersistedActiveRiddle();
    }
    renderActiveRiddle();
  }

  function renderActiveRiddle() {
    if (!elements.riddleCard || !elements.riddleText || !elements.riddleStatusBadge) {
      return;
    }
    const text = String(runtime.activeRiddleText || "").trim();
    const status = String(runtime.activeRiddleStatus || "idle").trim().toLowerCase();
    elements.riddleText.textContent = text;
    elements.riddleStatusBadge.textContent = riddleStatusLabel(status);
    elements.riddleStatusBadge.classList.toggle("is-live", status === "ongoing");
    elements.riddleCard.classList.toggle("is-visible", Boolean(text));
  }

  function persistCurrentTurn() {
    safeStorageSet(
      "haiguitang_current_turn",
      JSON.stringify({
        user: String(runtime.currentTurnUserText || "").trim(),
        agent: String(runtime.currentTurnAgentText || "").trim(),
      }),
    );
  }

  function clearPersistedCurrentTurn() {
    try {
      if (!window.localStorage || typeof window.localStorage.removeItem !== "function") {
        return;
      }
      window.localStorage.removeItem("haiguitang_current_turn");
    } catch (_error) {
    }
  }

  function setCurrentTurn(userText, agentText) {
    runtime.currentTurnUserText = String(userText || "").trim();
    runtime.currentTurnAgentText = String(agentText || "").trim();
    if (runtime.currentTurnUserText || runtime.currentTurnAgentText) {
      persistCurrentTurn();
    } else {
      clearPersistedCurrentTurn();
    }
    renderCurrentTurn();
  }

  function renderCurrentTurn() {
    if (!elements.turnCard || !elements.turnUserText || !elements.turnAgentText) {
      return;
    }

    const userText = String(runtime.currentTurnUserText || "").trim();
    const agentText = String(runtime.currentTurnAgentText || "").trim();
    elements.turnUserText.textContent = userText || "这一轮还没有提问。";
    elements.turnAgentText.textContent = agentText || "Agent 的完整回答会显示在这里。";
    elements.turnCard.classList.toggle("is-visible", Boolean(userText || agentText));
  }

  function deviceLabel(device, index) {
    const label = String(device && device.label || "").trim();
    if (label) {
      return label;
    }
    return index === 0 ? "系统默认麦克风" : `麦克风 ${index}`;
  }

  function renderAudioInputOptions() {
    if (!elements.audioInputSelect) {
      return;
    }

    const select = elements.audioInputSelect;
    const selected = normalizeAudioInputId(runtime.selectedAudioInputId);
    const options = [
      { value: "default", label: "系统默认麦克风" },
      ...runtime.audioInputDevices.map((device, index) => ({
        value: String(device.deviceId || "").trim(),
        label: deviceLabel(device, index + 1),
      })),
    ];

    select.innerHTML = "";
    options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      select.appendChild(node);
    });

    const hasSelected = options.some((option) => option.value === selected);
    select.value = hasSelected ? selected : "default";

    if (elements.audioInputHintText) {
      elements.audioInputHintText.textContent = selected === "default"
        ? "当前会使用浏览器或系统默认麦克风。如果想用 AirPods，请先在这里选中它。"
        : `当前已指定输入设备：${select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : "未知设备"}`;
    }
  }

  async function refreshAudioInputDevices() {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== "function") {
      if (elements.audioInputHintText) {
        elements.audioInputHintText.textContent = "当前浏览器不支持列出麦克风设备。";
      }
      return;
    }

    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      runtime.audioInputDevices = devices.filter((device) => device.kind === "audioinput");
      const selected = normalizeAudioInputId(runtime.selectedAudioInputId);
      if (selected !== "default" && !runtime.audioInputDevices.some((device) => device.deviceId === selected)) {
        runtime.selectedAudioInputId = "default";
        safeStorageSet("haiguitang_audio_input_id", "default");
      }
      renderAudioInputOptions();
    } catch (error) {
      if (elements.audioInputHintText) {
        elements.audioInputHintText.textContent = `读取麦克风设备失败：${error.message}`;
      }
    }
  }

  function renderDifficultyUi() {
    const difficulty = normalizeDifficulty(runtime.selectedDifficulty);
    const label = difficultyLabel(difficulty);

    if (elements.difficultyButton) {
      elements.difficultyButton.textContent = difficulty ? `${label}模式` : "选择难度";
    }
    if (elements.modePicker) {
      elements.modePicker.classList.toggle("is-open", Boolean(runtime.modePickerOpen));
      elements.modePicker.classList.toggle("is-busy", Boolean(runtime.roundStartPending));
    }
    if (elements.modePickerHelpText) {
      elements.modePickerHelpText.textContent = String(
        runtime.modePickerStatusText || "进入后会由 Agent 先说题面。你听完后点击“开始录音”，就能开始和 Agent 对话。",
      ).trim();
    }
    elements.modeButtons.forEach((button) => {
      const buttonDifficulty = normalizeDifficulty(button.dataset.difficulty || "");
      button.classList.toggle("is-selected", Boolean(difficulty) && buttonDifficulty === difficulty);
      button.disabled = Boolean(runtime.roundStartPending);
    });
  }

  function setModePickerOpen(open, statusText) {
    runtime.modePickerOpen = Boolean(open);
    if (typeof statusText === "string" && statusText.trim()) {
      runtime.modePickerStatusText = statusText.trim();
    } else if (!runtime.roundStartPending && !runtime.modePickerOpen) {
      runtime.modePickerStatusText = "进入后会由 Agent 先说题面。你听完后点击“开始录音”，就能开始和 Agent 对话。";
    }
    renderDifficultyUi();
  }

  function setSelectedDifficulty(value) {
    runtime.selectedDifficulty = normalizeDifficulty(value);
    renderDifficultyUi();
  }

  function updateUserSpeechOverlay() {
    if (!elements.userSpeechWrap || !elements.userSpeechText) {
      return;
    }

    const partial = String(runtime.sttPartialText || "").trim();
    const pending = combinedVoicePendingText();
    const stickyText = (
      runtime.voicePhase === "thinking" ||
      runtime.voicePhase === "speaking"
    )
      ? String(runtime.voiceLastUtterance || "").trim()
      : "";
    const text = partial || pending || stickyText;

    elements.userSpeechText.textContent = text;
    elements.userSpeechWrap.classList.toggle("is-visible", Boolean(text));
  }

  function clearVoiceDebounceTimer() {
    if (runtime.voiceDebounceTimer !== null) {
      window.clearTimeout(runtime.voiceDebounceTimer);
      runtime.voiceDebounceTimer = null;
    }
  }

  function clearVoiceRestartTimer() {
    if (runtime.voiceRestartTimer !== null) {
      window.clearTimeout(runtime.voiceRestartTimer);
      runtime.voiceRestartTimer = null;
    }
  }

  function resetVoicePendingSegments() {
    runtime.voicePendingSegments = [];
    updateUserSpeechOverlay();
  }

  function resetVoicePhaseClasses(element) {
    if (!element) {
      return;
    }
    element.classList.remove("is-active", "is-listening", "is-thinking", "is-speaking", "is-error");
  }

  function renderVoiceModeUi() {
    if (!elements.voiceModeButton || !elements.voiceModeBadge) {
      return;
    }

    const active = Boolean(runtime.voiceModeActive);
    const phase = String(runtime.voicePhase || "idle").trim().toLowerCase() || "idle";
    const hasLivePartial = Boolean(String(runtime.sttPartialText || "").trim());

    let buttonText = active ? "结束并发送" : "开始录音";
    let badgeText = "等待录音";
    let live = false;
    let phaseClass = "";
    let buttonDisabled = Boolean(runtime.roundStartPending);

    if (phase === "connecting" || phase === "stopping") {
      buttonText = "结束并发送";
      badgeText = phase === "stopping" ? "整理录音中" : "录音准备中";
      live = true;
    } else if (phase === "listening") {
      buttonText = "结束并发送";
      badgeText = hasLivePartial ? "录音识别中" : "录音进行中";
      live = true;
      phaseClass = "is-listening";
    } else if (phase === "thinking") {
      buttonText = "处理中";
      badgeText = "Agent 思考中";
      live = true;
      phaseClass = "is-thinking";
      buttonDisabled = true;
    } else if (phase === "speaking") {
      buttonText = "打断播报并录音";
      badgeText = "语音播报中";
      live = true;
      phaseClass = "is-speaking";
      buttonDisabled = false;
    } else if (phase === "error") {
      buttonText = "重新录音";
      badgeText = "录音异常";
      phaseClass = "is-error";
    }

    elements.voiceModeButton.textContent = buttonText;
    elements.voiceModeButton.disabled = buttonDisabled;
    elements.voiceModeBadge.textContent = badgeText;
    elements.voiceModeBadge.classList.toggle("is-live", live);

    resetVoicePhaseClasses(elements.voiceModeButton);
    resetVoicePhaseClasses(elements.voiceModeBadge);
    elements.voiceModeButton.classList.toggle("is-active", active);
    if (phaseClass) {
      elements.voiceModeButton.classList.add(phaseClass);
      elements.voiceModeBadge.classList.add(phaseClass);
    }
  }

  function setVoicePhase(phase) {
    runtime.voicePhase = String(phase || "idle").trim().toLowerCase() || "idle";
    renderVoiceModeUi();
    updateUserSpeechOverlay();
  }

  function combinedVoicePendingText() {
    return runtime.voicePendingSegments
      .map((entry) => String(entry && entry.text || "").trim())
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function queueVoicePendingSegment(payload) {
    const text = String(payload && payload.text || "").trim();
    if (!text || !runtime.voiceModeActive || runtime.voicePhase !== "listening") {
      return;
    }

    const resultId = String(payload && payload.resultId || "").trim() || `voice-${Date.now()}-${Math.random()}`;
    const existingIndex = runtime.voicePendingSegments.findIndex((entry) => entry.resultId === resultId);
    if (existingIndex >= 0) {
      runtime.voicePendingSegments[existingIndex] = { resultId, text };
    } else {
      runtime.voicePendingSegments.push({ resultId, text });
    }

    updateUserSpeechOverlay();
  }

  function capturedVoiceMessageText() {
    return [
      combinedVoicePendingText(),
      finalTranscriptText().replace(/\n+/g, " "),
      String(runtime.sttPartialText || "").trim(),
    ]
      .map((text) => String(text || "").trim())
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  async function waitForSttSessionToFinish(timeoutMs) {
    const deadline = Date.now() + Math.max(1000, Number(timeoutMs || 6000));
    while ((runtime.sttSocket || runtime.sttStarting) && Date.now() < deadline) {
      await new Promise((resolve) => {
        window.setTimeout(resolve, 80);
      });
    }
  }

  function extractVoiceReplyText(result) {
    const turn = result && result.turn ? result.turn : {};
    const scene = result && result.scene ? result.scene : {};
    const directive = scene && scene.directive ? scene.directive : {};
    const sceneState = scene && scene.state ? scene.state : null;
    return String(
      turn.reply ||
      directive.spoken_text ||
      (sceneState && sceneState.subtitle_text) ||
      directive.subtitle_text ||
      "",
    ).trim();
  }

  async function ensureTtsAudioContext() {
    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextCtor) {
      throw new Error("当前浏览器不支持语音播报所需的 Web Audio API");
    }
    if (!runtime.ttsAudioContext || runtime.ttsAudioContext.state === "closed") {
      runtime.ttsAudioContext = new AudioContextCtor();
    }
    if (runtime.ttsAudioContext.state === "suspended") {
      await runtime.ttsAudioContext.resume();
    }
    return runtime.ttsAudioContext;
  }

  function stopCurrentTtsPlayback(reason) {
    const cancel = runtime.ttsPlaybackCancel;
    runtime.ttsPlaybackCancel = null;
    if (typeof cancel === "function") {
      cancel(reason || "TTS playback cancelled");
      return;
    }

    const socket = runtime.ttsSocket;
    runtime.ttsSocket = null;
    if (socket) {
      try {
        socket.close(1000, "client_cleanup");
      } catch (_error) {
      }
    }

    Array.from(runtime.ttsActiveSources).forEach((source) => {
      try {
        source.stop(0);
      } catch (_error) {
      }
      try {
        source.disconnect();
      } catch (_error) {
      }
      runtime.ttsActiveSources.delete(source);
    });
    runtime.ttsNextStartTime = 0;
  }

  async function interruptSpeakingAndStartVoiceMode() {
    const isSpeaking = String(runtime.voicePhase || "").trim().toLowerCase() === "speaking";
    if (!isSpeaking) {
      await enableVoiceMode();
      return;
    }

    runtime.voiceModeActive = false;
    clearVoiceDebounceTimer();
    clearVoiceRestartTimer();
    stopCurrentTtsPlayback("voice_mode_interrupt");
    resetVoicePendingSegments();
    runtime.voiceLastUtterance = "";
    updateUserSpeechOverlay();
    setVoicePhase("idle");
    setStatus("已打断播报，正在准备录音...");
    log("已打断当前播报，准备开始新一轮录音。");

    await delay(VOICE_INTERRUPT_SETTLE_MS);
    await enableVoiceMode();
  }

  function decodePcm16Base64ToFloat32(base64Value) {
    const raw = String(base64Value || "").trim();
    if (!raw) {
      return new Float32Array(0);
    }
    const binary = window.atob(raw);
    const byteLength = binary.length - (binary.length % 2);
    const buffer = new ArrayBuffer(byteLength);
    const bytes = new Uint8Array(buffer);
    for (let index = 0; index < byteLength; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    const view = new DataView(buffer);
    const sampleCount = byteLength / 2;
    const output = new Float32Array(sampleCount);
    for (let i = 0; i < sampleCount; i += 1) {
      output[i] = view.getInt16(i * 2, true) / 0x8000;
    }
    return output;
  }

  async function playSpeechWithTts(text) {
    const inputText = String(text || "").trim();
    if (!inputText) {
      return;
    }

    const audioContext = await ensureTtsAudioContext();
    stopCurrentTtsPlayback("superseded");
    runtime.ttsNextStartTime = audioContext.currentTime;

    return new Promise((resolve, reject) => {
      let settled = false;
      let sawAudio = false;
      let streamDone = false;
      let pendingSources = 0;
      let finishTimer = null;

      const socket = new WebSocket(resolveWebSocketUrl("api/v1/ws/tts"));
      runtime.ttsSocket = socket;

      function cleanup() {
        if (runtime.ttsSocket === socket) {
          runtime.ttsSocket = null;
        }
        if (runtime.ttsPlaybackCancel === cancelPlayback) {
          runtime.ttsPlaybackCancel = null;
        }
      }

      function clearFinishTimer() {
        if (finishTimer !== null) {
          window.clearTimeout(finishTimer);
          finishTimer = null;
        }
      }

      function settle(ok, value) {
        if (settled) {
          return;
        }
        settled = true;
        clearFinishTimer();
        if (!ok) {
          try {
            if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
              socket.close(1000, "playback_failed");
            }
          } catch (_error) {
          }
        } else {
          window.setTimeout(() => {
            try {
              if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
                socket.close(1000, "playback_complete");
              }
            } catch (_error) {
            }
          }, 80);
        }
        cleanup();
        if (ok) {
          resolve(value);
          return;
        }
        reject(value instanceof Error ? value : new Error(String(value || "TTS 播放失败")));
      }

      function maybeFinish() {
        if (!streamDone || pendingSources !== 0 || finishTimer !== null) {
          return;
        }
        finishTimer = window.setTimeout(() => {
          finishTimer = null;
          settle(true, null);
        }, TTS_PLAYBACK_TAIL_PAD_MS);
      }

      function cancelPlayback(cancelReason) {
        if (settled) {
          return;
        }
        clearFinishTimer();
        Array.from(runtime.ttsActiveSources).forEach((source) => {
          try {
            source.stop(0);
          } catch (_error) {
          }
          try {
            source.disconnect();
          } catch (_error) {
          }
          runtime.ttsActiveSources.delete(source);
        });
        runtime.ttsNextStartTime = audioContext.currentTime;
        try {
          if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
            socket.close(1000, "playback_cancelled");
          }
        } catch (_error) {
        }
        settle(false, new Error(String(cancelReason || "TTS 播放已取消")));
      }

      function queueAudioChunk(base64Value, sampleRate) {
        clearFinishTimer();
        const floatSamples = decodePcm16Base64ToFloat32(base64Value);
        if (!floatSamples.length) {
          return;
        }
        const playbackRate = Math.max(8000, Number(sampleRate || 0) || 24000);
        const buffer = audioContext.createBuffer(1, floatSamples.length, playbackRate);
        buffer.copyToChannel(floatSamples, 0, 0);

        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);

        const startAt = Math.max(audioContext.currentTime + 0.02, runtime.ttsNextStartTime);
        runtime.ttsNextStartTime = startAt + buffer.duration;
        pendingSources += 1;
        runtime.ttsActiveSources.add(source);

        source.onended = () => {
          pendingSources = Math.max(0, pendingSources - 1);
          runtime.ttsActiveSources.delete(source);
          maybeFinish();
        };

        source.start(startAt);
      }

      runtime.ttsPlaybackCancel = cancelPlayback;

      socket.addEventListener("open", () => {
        socket.send(JSON.stringify({ type: "speak", text: inputText }));
      });

      socket.addEventListener("message", (event) => {
        let payload = null;
        try {
          payload = JSON.parse(String(event.data || ""));
        } catch (_error) {
          return;
        }
        if (!payload || typeof payload !== "object") {
          return;
        }

        const type = String(payload.type || "").trim().toLowerCase();
        const data = payload.data && typeof payload.data === "object" ? payload.data : {};

        if (type === "tts_started" || type === "tts_session_ready" || type === "tts_result") {
          return;
        }

        if (type === "audio_chunk") {
          sawAudio = true;
          try {
            queueAudioChunk(payload.pcm16_base64, payload.sample_rate);
          } catch (error) {
            cancelPlayback(error.message);
          }
          return;
        }

        if (type === "done" || type === "interrupted") {
          streamDone = true;
          maybeFinish();
          return;
        }

        if (type === "tts_unavailable") {
          settle(
            false,
            new Error(String(data.error || "后端 TTS 当前不可用").trim() || "后端 TTS 当前不可用"),
          );
          return;
        }

        if (type === "error") {
          const messageText = String(payload.message || "TTS 播放失败").trim();
          if (sawAudio) {
            log(`TTS 途中返回错误：${messageText}`);
            streamDone = true;
            maybeFinish();
            return;
          }
          settle(false, new Error(messageText));
        }
      });

      socket.addEventListener("close", (event) => {
        if (settled) {
          return;
        }
        if (sawAudio) {
          streamDone = true;
          maybeFinish();
          return;
        }
        settle(false, new Error(`TTS WebSocket 已关闭 (code=${Number(event.code || 0)})`));
      });

      socket.addEventListener("error", () => {
        if (settled || sawAudio) {
          return;
        }
        settle(false, new Error("TTS WebSocket 连接失败"));
      });
    });
  }

  async function resumeVoiceListening() {
    clearVoiceRestartTimer();
    if (!runtime.voiceModeActive) {
      return;
    }
    if (runtime.sttSocket || runtime.sttStarting) {
      return;
    }

    resetVoicePendingSegments();
    runtime.sttPartialText = "";
    renderSttTranscript();
    setVoicePhase("connecting");
    setStatus("录音准备中，马上就可以说话。");
    await startSttCapture();
  }

  function scheduleVoiceListeningResume(delayMs) {
    void delayMs;
  }

  async function submitVoicePendingUtterance() {
    clearVoiceDebounceTimer();
    if (!runtime.voiceModeActive) {
      return false;
    }

    const message = capturedVoiceMessageText();
    if (!message) {
      return false;
    }

    const turnSerial = ++runtime.voiceTurnSerial;
    resetVoicePendingSegments();
    runtime.voiceLastUtterance = message;
    if (elements.agentPromptInput) {
      elements.agentPromptInput.value = message;
    }
    updateUserSpeechOverlay();
    setVoicePhase("thinking");
    setStatus("已收到语音，正在发给 Agent...");
    log(`录音发送：${message}`);

    try {
      const result = await triggerAgentTurn(message);
      if (!runtime.voiceModeActive || turnSerial !== runtime.voiceTurnSerial) {
        return false;
      }

      const replyText = extractVoiceReplyText(result);
      if (replyText) {
        setVoicePhase("speaking");
        setStatus("Agent 正在语音播报...");
        await playSpeechWithTts(replyText);
      } else {
        log("Agent 回复没有可播报文本，已跳过 TTS。");
      }

      if (!runtime.voiceModeActive || turnSerial !== runtime.voiceTurnSerial) {
        return false;
      }
      runtime.voiceModeActive = false;
      setVoicePhase("idle");
      setStatus("这一轮语音问答已结束，可以开始下一次录音。");
      return true;
    } catch (error) {
      if (isTtsCancellationError(error)) {
        log(`语音播报已取消：${ttsCancellationReason(error) || "unknown"}`);
        return false;
      }
      if (turnSerial !== runtime.voiceTurnSerial) {
        return false;
      }
      runtime.voiceModeActive = false;
      setVoicePhase("error");
      setStatus(`录音发送失败：${error.message}`);
      log(`录音发送失败：${error.message}`);
      return false;
    }
  }

  async function startRoundWithDifficulty(difficulty) {
    const normalizedDifficulty = normalizeDifficulty(difficulty);
    if (!normalizedDifficulty) {
      throw new Error("未知难度选项");
    }

    const label = difficultyLabel(normalizedDifficulty);
    runtime.roundStartPending = true;
    setSelectedDifficulty(normalizedDifficulty);
    setModePickerOpen(true, `正在准备${label}难度题面...`);

    try {
      await disableVoiceMode();
    } catch (_error) {
    }
    stopCurrentTtsPlayback("round_restart");
    resetVoicePendingSegments();
    runtime.voiceLastUtterance = "";
    updateUserSpeechOverlay();
    resetSttTranscript();
    setCurrentTurn("", "");

    setStatus(`正在生成${label}难度题面...`);
    log(`正在开始${label}难度新一局。`);

    try {
      const result = await requestJson("api/v1/haiguitang/start-round", {
        timeoutMs: AGENT_REQUEST_TIMEOUT_MS,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ difficulty: normalizedDifficulty }),
      });

      const presentation = await applyAgentTurnResult(result, {
        originLabel: `${label}难度开局`,
        completedStatus: `${label}难度已开始。`,
        updateRiddle: false,
      });
      const openingText = String(presentation.spokenText || "").trim();
      setActiveRiddle(openingText, "ongoing");

      setModePickerOpen(false);
      runtime.roundStartPending = false;
      renderDifficultyUi();
      if (openingText) {
        setVoicePhase("speaking");
        setStatus(`${label}难度开场播报中...`);
        await playSpeechWithTts(openingText);
      }

      if (!runtime.voiceModeActive) {
        setVoicePhase("idle");
        setStatus(`${label}难度题面已播报，点击“开始录音”后就能和 Agent 对话。`);
        log(`${label}难度题面已播报，等待点击开始录音。`);
      }
    } catch (error) {
      if (isTtsCancellationError(error)) {
        const reason = ttsCancellationReason(error);
        if (reason === "voice_mode_interrupt" || reason === "voice_mode_start") {
          log(`${label}难度开场播报已被手动打断，正在切到录音状态。`);
        } else if (reason === "round_restart") {
          log(`${label}难度开场播报已被新的难度选择覆盖。`);
        } else {
          log(`${label}难度开场播报已取消：${reason || "unknown"}`);
        }
        return;
      }
      setVoicePhase("error");
      setModePickerOpen(true, `开局失败：${error.message}`);
      setStatus(`开局失败：${error.message}`);
      log(`开局失败：${error.message}`);
      throw error;
    } finally {
      runtime.roundStartPending = false;
      renderDifficultyUi();
    }
  }

  async function enableVoiceMode(options) {
    const settings = {
      skipDifficultyCheck: false,
      ...options,
    };
    if (runtime.voiceModeActive) {
      return;
    }
    if (!settings.skipDifficultyCheck && !normalizeDifficulty(runtime.selectedDifficulty)) {
      setModePickerOpen(true, "请先选择简单 / 中等 / 困难，再开始录音。");
      setStatus("请先选择难度。");
      return;
    }

    runtime.voiceModeActive = true;
    runtime.voiceTurnSerial += 1;
    clearVoiceDebounceTimer();
    clearVoiceRestartTimer();
    resetVoicePendingSegments();
    stopCurrentTtsPlayback("voice_mode_start");
    resetSttTranscript();
    runtime.voiceLastUtterance = "";
    updateUserSpeechOverlay();

    if (runtime.sttSocket || runtime.sttStarting) {
      try {
        await stopSttCapture({
          notifyServer: false,
          reasonLabel: "正在切换录音状态。",
        });
      } catch (_error) {
      }
    }

    await ensureTtsAudioContext();
    setVoicePhase("connecting");
    setStatus("正在开始本次录音...");
    log("已开始录音。");
    await resumeVoiceListening();
  }

  async function disableVoiceMode() {
    if (!runtime.voiceModeActive && runtime.voicePhase === "idle") {
      return;
    }

    clearVoiceDebounceTimer();
    clearVoiceRestartTimer();
    stopCurrentTtsPlayback("voice_mode_disabled");
    const shouldSubmitRecordedUtterance =
      Boolean(runtime.sttSocket) ||
      Boolean(runtime.sttStarting) ||
      runtime.voicePhase === "connecting" ||
      runtime.voicePhase === "listening" ||
      runtime.voicePhase === "stopping";

    if (!shouldSubmitRecordedUtterance) {
      runtime.voiceModeActive = false;
      resetVoicePendingSegments();
      runtime.voiceLastUtterance = "";
      updateUserSpeechOverlay();
      setVoicePhase("idle");
      setStatus("当前录音已取消。");
      log("已取消当前录音。");
      return;
    }

    setVoicePhase("stopping");

    try {
      await stopSttCapture({
        notifyServer: true,
        reasonLabel: "本次录音已结束。",
      });
    } catch (_error) {
    }

    await waitForSttSessionToFinish(7000);

    const message = capturedVoiceMessageText();
    if (!message) {
      runtime.voiceModeActive = false;
      resetVoicePendingSegments();
      runtime.voiceLastUtterance = "";
      updateUserSpeechOverlay();
      setVoicePhase("idle");
      setStatus("没有识别到有效语音，可以重新录一轮。");
      log("录音结束，但没有拿到可发送的文本。");
      return;
    }

    await submitVoicePendingUtterance();
  }

  function showSubtitle(message) {
    const text = String(message || "").trim();
    elements.subtitleText.textContent = text;
    elements.subtitleText.classList.toggle("is-visible", text.length > 0);
  }

  function showFallback(title, body) {
    elements.fallbackTitle.textContent = title;
    elements.fallbackBody.textContent = body;
    elements.sceneFallback.classList.remove("is-hidden");
    elements.sceneVideo.pause();
    elements.sceneVideo.removeAttribute("src");
    elements.sceneVideo.load();
  }

  function hideFallback() {
    elements.sceneFallback.classList.add("is-hidden");
  }

  async function requestJson(path, options) {
    const settings = {
      timeoutMs: REQUEST_TIMEOUT_MS,
      ...(options || {}),
    };
    const timeoutMs = Math.max(500, Number(settings.timeoutMs || REQUEST_TIMEOUT_MS));
    delete settings.timeoutMs;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = controller
      ? window.setTimeout(() => controller.abort(), timeoutMs)
      : null;
    try {
      const response = await fetch(resolveApiUrl(path), {
        method: "GET",
        headers: {
          Accept: "application/json",
        },
        cache: "no-store",
        signal: controller ? controller.signal : undefined,
        ...settings,
      });
      const text = await response.text();
      let payload = null;
      try {
        payload = text ? JSON.parse(text) : null;
      } catch (_error) {
        payload = null;
      }
      if (!response.ok || !payload || payload.ok !== true) {
        const errorMessage =
          (payload && payload.error && payload.error.message) ||
          (payload && payload.message) ||
          text ||
          `HTTP ${response.status}`;
        throw new Error(errorMessage);
      }
      return payload.data;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error(`请求超时，请检查接口地址是否可达：${runtime.apiBase}`);
      }
      throw error;
    } finally {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    }
  }

  function resolveWebSocketUrl(path) {
    const url = new URL(path, runtime.apiBase || defaultApiBase());
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  function setSttState(label, metaText, live) {
    elements.sttStateBadge.textContent = String(label || "未连接");
    elements.sttStateBadge.classList.toggle("is-live", Boolean(live));
    if (typeof metaText === "string") {
      elements.sttMetaText.textContent = metaText;
    }
    refreshSttControls();
  }

  function refreshSttControls() {
    const sessionActive = Boolean(runtime.sttSocket) || Boolean(runtime.sttStarting);
    const voiceManaged = Boolean(runtime.voiceModeActive);
    elements.sttStartButton.disabled = sessionActive || voiceManaged;
    elements.sttStopButton.disabled = !sessionActive || voiceManaged;
    elements.sttUsePromptButton.disabled = !finalTranscriptText() && !String(runtime.sttPartialText || "").trim();
  }

  function finalTranscriptText() {
    return runtime.sttFinalSegments
      .map((entry) => String(entry && entry.text || "").trim())
      .filter(Boolean)
      .join("\n");
  }

  function renderSttTranscript() {
    const finalText = finalTranscriptText();
    const partialText = String(runtime.sttPartialText || "").trim();
    elements.sttFinalText.textContent = finalText || "还没有转写结果。";
    elements.sttPartialText.textContent = partialText || "等待你开始说话。";
    updateUserSpeechOverlay();
    refreshSttControls();
  }

  function resetSttTranscript() {
    runtime.sttFinalSegments = [];
    runtime.sttPartialText = "";
    runtime.voiceLastUtterance = "";
    renderSttTranscript();
  }

  function upsertFinalTranscript(payload) {
    const text = String(payload && payload.text || "").trim();
    if (!text) {
      return;
    }
    const resultId = String(payload && payload.resultId || "").trim();
    if (!resultId) {
      runtime.sttFinalSegments.push({ resultId: `final-${Date.now()}-${Math.random()}`, text });
      return;
    }
    const existingIndex = runtime.sttFinalSegments.findIndex((entry) => entry.resultId === resultId);
    if (existingIndex >= 0) {
      runtime.sttFinalSegments[existingIndex] = { resultId, text };
      return;
    }
    runtime.sttFinalSegments.push({ resultId, text });
  }

  function mergeInt16Arrays(left, right) {
    const a = left instanceof Int16Array ? left : new Int16Array(0);
    const b = right instanceof Int16Array ? right : new Int16Array(0);
    if (a.length === 0) {
      return b.slice();
    }
    if (b.length === 0) {
      return a.slice();
    }
    const merged = new Int16Array(a.length + b.length);
    merged.set(a, 0);
    merged.set(b, a.length);
    return merged;
  }

  function downsampleFloat32ToInt16(input, inputSampleRate, outputSampleRate) {
    const source = input instanceof Float32Array ? input : new Float32Array(0);
    if (!source.length) {
      return new Int16Array(0);
    }

    if (outputSampleRate === inputSampleRate) {
      const direct = new Int16Array(source.length);
      for (let i = 0; i < source.length; i += 1) {
        const clamped = Math.max(-1, Math.min(1, source[i]));
        direct[i] = clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
      }
      return direct;
    }

    const ratio = inputSampleRate / outputSampleRate;
    const outputLength = Math.max(1, Math.floor(source.length / ratio));
    const output = new Int16Array(outputLength);
    let offsetResult = 0;
    let offsetBuffer = 0;

    while (offsetResult < output.length) {
      const nextOffsetBuffer = Math.min(source.length, Math.round((offsetResult + 1) * ratio));
      let sum = 0;
      let count = 0;
      for (let index = offsetBuffer; index < nextOffsetBuffer; index += 1) {
        sum += source[index];
        count += 1;
      }
      const sample = count > 0 ? sum / count : 0;
      const clamped = Math.max(-1, Math.min(1, sample));
      output[offsetResult] = clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
      offsetResult += 1;
      offsetBuffer = nextOffsetBuffer;
    }

    return output;
  }

  function encodeInt16LittleEndian(samples) {
    const pcm = samples instanceof Int16Array ? samples : new Int16Array(0);
    const buffer = new ArrayBuffer(pcm.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < pcm.length; i += 1) {
      view.setInt16(i * 2, pcm[i], true);
    }
    return buffer;
  }

  function sendPendingSttSamples(forceFlush) {
    const socket = runtime.sttSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN || !runtime.sttCanSendAudio) {
      return;
    }

    while (runtime.sttPendingSamples.length >= STT_CHUNK_SAMPLES) {
      const chunk = runtime.sttPendingSamples.slice(0, STT_CHUNK_SAMPLES);
      runtime.sttPendingSamples = runtime.sttPendingSamples.slice(STT_CHUNK_SAMPLES);
      socket.send(encodeInt16LittleEndian(chunk));
    }

    if (forceFlush && runtime.sttPendingSamples.length > 0) {
      socket.send(encodeInt16LittleEndian(runtime.sttPendingSamples));
      runtime.sttPendingSamples = new Int16Array(0);
    }
  }

  function cleanupSttAudioGraph() {
    runtime.sttCanSendAudio = false;
    runtime.sttPendingSamples = new Int16Array(0);

    if (runtime.sttProcessorNode) {
      try {
        runtime.sttProcessorNode.onaudioprocess = null;
      } catch (_error) {
      }
      try {
        runtime.sttProcessorNode.disconnect();
      } catch (_error) {
      }
      runtime.sttProcessorNode = null;
    }

    if (runtime.sttSourceNode) {
      try {
        runtime.sttSourceNode.disconnect();
      } catch (_error) {
      }
      runtime.sttSourceNode = null;
    }

    if (runtime.sttMediaStream) {
      try {
        runtime.sttMediaStream.getTracks().forEach((track) => track.stop());
      } catch (_error) {
      }
      runtime.sttMediaStream = null;
    }

    if (runtime.sttAudioContext) {
      try {
        runtime.sttAudioContext.close();
      } catch (_error) {
      }
      runtime.sttAudioContext = null;
    }
  }

  function cleanupSttSocket() {
    const socket = runtime.sttSocket;
    runtime.sttSocket = null;
    if (!socket) {
      return;
    }
    try {
      socket.onopen = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.onmessage = null;
    } catch (_error) {
    }
    try {
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close(1000, "client_cleanup");
      }
    } catch (_error) {
    }
  }

  function finishSttSession(label, metaText, live) {
    cleanupSttAudioGraph();
    cleanupSttSocket();
    runtime.sttStarting = false;
    runtime.sttSessionId = "";
    runtime.sttCurrentConfig = null;
    setSttState(label, metaText, live);
  }

  function fillTranscriptIntoAgentInput() {
    const finalText = finalTranscriptText();
    const fallbackText = String(runtime.sttPartialText || "").trim();
    const text = finalText || fallbackText;
    if (!text) {
      throw new Error("还没有可用的转写文本");
    }
    elements.agentPromptInput.value = text;
    log("已把 STT 文本填入 Agent 输入框。");
  }

  async function stopSttCapture(options) {
    const settings = {
      notifyServer: true,
      reasonLabel: "听写已停止。",
      ...options,
    };
    sendPendingSttSamples(true);
    cleanupSttAudioGraph();
    runtime.sttStarting = false;

    const socket = runtime.sttSocket;
    if (settings.notifyServer && socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: "stop" }));
        setSttState("停止中", "正在等待 AWS 返回最后一段结果...", false);
        return;
      } catch (error) {
        log(`发送 STT 停止指令失败：${error.message}`);
      }
    }

    finishSttSession("已停止", settings.reasonLabel, false);
  }

  async function startSttCapture() {
    if (runtime.sttSocket || runtime.sttStarting) {
      return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      throw new Error("当前浏览器不支持麦克风采集");
    }

    runtime.sttStarting = true;
    setSttState("准备中", "正在申请麦克风权限...", false);

    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextCtor) {
      runtime.sttStarting = false;
      throw new Error("当前浏览器不支持 Web Audio API");
    }

    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: runtime.selectedAudioInputId !== "default"
            ? { exact: runtime.selectedAudioInputId }
            : undefined,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      await refreshAudioInputDevices();
      const audioContext = new AudioContextCtor();
      const sourceNode = audioContext.createMediaStreamSource(mediaStream);
      const processorNode = audioContext.createScriptProcessor(4096, 1, 1);
      sourceNode.connect(processorNode);
      processorNode.connect(audioContext.destination);
      processorNode.onaudioprocess = (event) => {
        if (!runtime.sttCanSendAudio) {
          return;
        }
        const channelData = event.inputBuffer.getChannelData(0);
        const pcm = downsampleFloat32ToInt16(channelData, audioContext.sampleRate, STT_TARGET_SAMPLE_RATE);
        runtime.sttPendingSamples = mergeInt16Arrays(runtime.sttPendingSamples, pcm);
        sendPendingSttSamples(false);
      };

      const socket = new WebSocket(resolveWebSocketUrl("api/v1/ws/stt"));
      socket.binaryType = "arraybuffer";
      runtime.sttMediaStream = mediaStream;
      runtime.sttAudioContext = audioContext;
      runtime.sttSourceNode = sourceNode;
      runtime.sttProcessorNode = processorNode;
      runtime.sttSocket = socket;

      socket.addEventListener("message", async (event) => {
        let payload = null;
        try {
          payload = JSON.parse(String(event.data || ""));
        } catch (_error) {
          return;
        }
        if (!payload || typeof payload !== "object") {
          return;
        }

        const type = String(payload.type || "").trim().toLowerCase();
        const data = payload.data && typeof payload.data === "object" ? payload.data : {};
        if (type === "ready") {
          runtime.sttCurrentConfig = data.config || null;
          const expected = data.expected_audio || {};
          const metaText = `已连接 STT 网关，目标音频：${expected.sampleRateHertz || STT_TARGET_SAMPLE_RATE}Hz / ${expected.mediaEncoding || "pcm"} / mono`;
          setSttState("已连接", metaText, false);
          socket.send(JSON.stringify({
            type: "start",
            languageCode: "zh-CN",
            mediaEncoding: "pcm",
            sampleRateHertz: STT_TARGET_SAMPLE_RATE,
            partialResultsStability: "medium",
          }));
          return;
        }

        if (type === "stream_started") {
          runtime.sttStarting = false;
          runtime.sttCanSendAudio = true;
          runtime.sttSessionId = String(data.sessionId || "").trim();
          setSttState("听写中", "AWS Transcribe 已开始接收音频，直接说话就可以。", true);
          if (runtime.voiceModeActive) {
            setVoicePhase("listening");
            setStatus("录音中，结束后会一次性发送给 Agent。");
          }
          log("实时 STT 已启动。");
          return;
        }

        if (type === "partial") {
          runtime.sttPartialText = String(data.text || "").trim();
          renderSttTranscript();
          if (runtime.voiceModeActive && runtime.voicePhase === "listening") {
            renderVoiceModeUi();
          }
          return;
        }

        if (type === "final") {
          runtime.sttPartialText = "";
          upsertFinalTranscript(data);
          renderSttTranscript();
          queueVoicePendingSegment(data);
          return;
        }

        if (type === "stream_stopped") {
          const finalCount = Number(data.finalSegments || 0);
          const audioBytes = Number(data.audioBytes || 0);
          finishSttSession(
            "已停止",
            `本轮结束，final segments=${finalCount}，上传音频约 ${audioBytes} bytes。`,
            false,
          );
          log("实时 STT 已停止。");
          if (runtime.voiceModeActive && (runtime.voicePhase === "connecting" || runtime.voicePhase === "listening")) {
            scheduleVoiceListeningResume(VOICE_LISTEN_RESUME_DELAY_MS);
          }
          return;
        }

        if (type === "status") {
          runtime.sttCurrentConfig = data.config || runtime.sttCurrentConfig;
          return;
        }

        if (type === "error") {
          const messageText = String(payload.message || "实时 STT 出错").trim();
          finishSttSession("出错", messageText, false);
          log(`实时 STT 出错：${messageText}`);
          if (runtime.voiceModeActive && (runtime.voicePhase === "connecting" || runtime.voicePhase === "listening")) {
            setVoicePhase("error");
            scheduleVoiceListeningResume(1200);
          }
        }
      });

      socket.addEventListener("close", () => {
        if (socket !== runtime.sttSocket) {
          return;
        }
        finishSttSession("已断开", "STT WebSocket 已关闭。", false);
        if (runtime.voiceModeActive && (runtime.voicePhase === "connecting" || runtime.voicePhase === "listening")) {
          setVoicePhase("error");
          scheduleVoiceListeningResume(1200);
        }
      });

      socket.addEventListener("error", () => {
        if (socket !== runtime.sttSocket) {
          return;
        }
        finishSttSession("异常", "STT WebSocket 连接失败。", false);
        log("实时 STT WebSocket 连接失败。");
        if (runtime.voiceModeActive && (runtime.voicePhase === "connecting" || runtime.voicePhase === "listening")) {
          setVoicePhase("error");
          scheduleVoiceListeningResume(1200);
        }
      });

      setSttState("连接中", "正在建立 STT WebSocket...", false);
    } catch (error) {
      finishSttSession("未连接", "无法启动实时 STT。", false);
      if (runtime.selectedAudioInputId !== "default") {
        const detail = String((error && error.message) || error || "").trim();
        if (elements.audioInputHintText) {
          elements.audioInputHintText.textContent =
            `指定麦克风启动失败，可能是设备断开或没有权限：${detail || "unknown error"}`;
        }
      }
      throw error;
    }
  }

  function resolveSceneUrl(rawValue) {
    const raw = String(rawValue || "").trim();
    if (!raw) {
      return "";
    }
    try {
      return new URL(raw, runtime.apiBase).toString();
    } catch (_error) {
      return raw;
    }
  }

  function readConfigVideoUrl(key) {
    const source = runtime.config && runtime.config[key];
    return resolveSceneUrl(source);
  }

  function defaultLoopUrl(sceneState) {
    return (
      resolveSceneUrl(sceneState && sceneState.default_video_url) ||
      resolveSceneUrl(sceneState && sceneState.video_url) ||
      readConfigVideoUrl("default_video_url") ||
      readConfigVideoUrl("intro_video_url")
    );
  }

  function sceneStateIsCustom(sceneState) {
    if (!sceneState) {
      return false;
    }
    return Boolean(
      Number(sceneState.version || 0) > 0 ||
      String(sceneState.clip || "default").trim().toLowerCase() !== "default" ||
      String(sceneState.subtitle_text || "").trim() ||
      String(sceneState.video_url || "").trim()
    );
  }

  function currentClip() {
    return String(
      (runtime.latestSceneState && runtime.latestSceneState.clip) || "default",
    ).trim().toLowerCase() || "default";
  }

  function currentLoopPlayback() {
    if (!runtime.latestSceneState) {
      return true;
    }
    return runtime.latestSceneState.loop_playback !== false;
  }

  async function playVideo(url, options) {
    const settings = {
      clip: "default",
      loop: false,
      status: "正在准备视频...",
      fallbackTitle: "视频不可用",
      fallbackBody: "当前视频还没准备好。",
      onEnded: null,
      onUnavailable: null,
      ...options,
    };

    setClipBadge(settings.clip);
    setStatus(settings.status);

    if (!url) {
      showFallback(settings.fallbackTitle, settings.fallbackBody);
      return;
    }

    const token = ++runtime.playToken;
    hideFallback();
    elements.sceneVideo.pause();
    elements.sceneVideo.autoplay = true;
    elements.sceneVideo.defaultMuted = true;
    elements.sceneVideo.loop = Boolean(settings.loop);
    elements.sceneVideo.muted = true;
    elements.sceneVideo.volume = 0;
    elements.sceneVideo.playsInline = true;
    elements.sceneVideo.src = url;
    elements.sceneVideo.load();

    elements.sceneVideo.onended = () => {
      if (token !== runtime.playToken) {
        return;
      }
      if (typeof settings.onEnded === "function") {
        settings.onEnded();
      }
    };

    elements.sceneVideo.onerror = async () => {
      if (token !== runtime.playToken) {
        return;
      }
      if (typeof settings.onUnavailable === "function") {
        await settings.onUnavailable({
          kind: "media_error",
          url,
          message: `视频加载失败：${url}`,
        });
        return;
      }
      showFallback(settings.fallbackTitle, settings.fallbackBody);
      log(`视频加载失败：${url}`);
    };

    try {
      await elements.sceneVideo.play();
    } catch (error) {
      if (token !== runtime.playToken) {
        return;
      }
      if (String((error && error.name) || "").trim() === "AbortError") {
        log(`视频切换已被新的片段接管：${url}`);
        return;
      }
      if (typeof settings.onUnavailable === "function") {
        await settings.onUnavailable({
          kind: "play_rejected",
          url,
          message: String((error && error.message) || error || "视频播放失败").trim(),
        });
        return;
      }
      showFallback(
        settings.fallbackTitle,
        `${settings.fallbackBody} 浏览器阻止了自动播放，请点右上角“沉浸模式”后再试一次。`,
      );
      log(`自动播放失败：${error.message}`);
    }
  }

  async function playDefaultLoop(sceneState) {
    const videoUrl = defaultLoopUrl(sceneState);
    await playVideo(videoUrl, {
      clip: "default",
      loop: true,
      status: "角色已入戏，等待下一次切换。",
      fallbackTitle: "等待默认角色视频",
      fallbackBody: "默认角色视频暂时还播不起来。请先确认 `default.mp4` 或 `begin.mp4` 能在浏览器里正常打开。",
    });
  }

  async function fallbackToDefaultLoop(sceneState, message) {
    const detail = String(message || "").trim();
    if (detail) {
      log(detail);
      setStatus(detail);
    }
    try {
      await playDefaultLoop(sceneState);
    } catch (error) {
      log(`回退默认角色视频失败：${error.message}`);
      throw error;
    }
  }

  async function playIntro(url) {
    const introUrl = resolveSceneUrl(url) || readConfigVideoUrl("intro_video_url");
    if (!introUrl) {
      await playDefaultLoop(runtime.latestSceneState);
      return;
    }

    await playVideo(introUrl, {
      clip: "intro",
      loop: false,
      status: "片头播放中...",
      fallbackTitle: "片头暂不可用",
      fallbackBody: "片头暂时播不起来，正在回退到默认角色视频。",
      onEnded: () => {
        playDefaultLoop(runtime.latestSceneState);
      },
      onUnavailable: async (reason) => {
        await fallbackToDefaultLoop(
          runtime.latestSceneState,
          `片头播放失败，已回退到默认角色视频。${reason && reason.message ? ` ${reason.message}` : ""}`.trim(),
        );
      },
    });
  }

  async function applySceneState(sceneState, force) {
    if (!sceneState) {
      return;
    }

    const sceneVersion = Number(sceneState.version || 0);
    if (!force && sceneVersion === runtime.lastVersion) {
      return;
    }

    runtime.latestSceneState = sceneState;
    runtime.lastVersion = sceneVersion;
    showSubtitle(sceneState.subtitle_text || "");

    const clip = String(sceneState.clip || "default").trim().toLowerCase();
    const resolvedUrl = resolveSceneUrl(sceneState.video_url);
    const shouldLoop = sceneState.loop_playback !== false;

    if (clip === "intro") {
      await playIntro(resolvedUrl || readConfigVideoUrl("intro_video_url"));
      return;
    }

    if (clip === "nod") {
      await playVideo(resolvedUrl || readConfigVideoUrl("nod_video_url"), {
        clip: "nod",
        loop: shouldLoop,
        status: "切换到点头反馈。",
        fallbackTitle: "点头表情未就绪",
        fallbackBody: "点头表情暂时播不起来，正在回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
        },
        onUnavailable: async (reason) => {
          await fallbackToDefaultLoop(
            sceneState,
            `点头片段播放失败，已回退到默认角色视频。${reason && reason.message ? ` ${reason.message}` : ""}`.trim(),
          );
        },
      });
      return;
    }

    if (clip === "shake") {
      await playVideo(resolvedUrl || readConfigVideoUrl("shake_video_url"), {
        clip: "shake",
        loop: shouldLoop,
        status: "切换到摇头反馈。",
        fallbackTitle: "摇头表情未就绪",
        fallbackBody: "摇头表情暂时播不起来，正在回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
        },
        onUnavailable: async (reason) => {
          await fallbackToDefaultLoop(
            sceneState,
            `摇头片段播放失败，已回退到默认角色视频。${reason && reason.message ? ` ${reason.message}` : ""}`.trim(),
          );
        },
      });
      return;
    }

    if (clip === "outro") {
      await playVideo(resolvedUrl || readConfigVideoUrl("outro_video_url"), {
        clip: "outro",
        loop: shouldLoop,
        status: "结束片段播放中。",
        fallbackTitle: "结束片段未就绪",
        fallbackBody: "结束片段暂时播不起来，正在回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
        },
        onUnavailable: async (reason) => {
          await fallbackToDefaultLoop(
            sceneState,
            `结束片段播放失败，已回退到默认角色视频。${reason && reason.message ? ` ${reason.message}` : ""}`.trim(),
          );
        },
      });
      return;
    }

    await playDefaultLoop(sceneState);
  }

  async function presentSceneState(payload) {
    const nextSceneState = await requestJson("api/v1/scenes/haiguitang/state", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });
    log(`已发送场景切换：${payload.clip || "default"}`);
    await applySceneState(nextSceneState, true);
  }

  async function applyAgentTurnResult(result, options) {
    const settings = {
      originLabel: "Agent",
      completedStatus: "Agent 已完成这一轮互动。",
      ...options,
    };
    const turn = result && result.turn ? result.turn : {};
    const scene = result && result.scene ? result.scene : {};
    const directive = scene && scene.directive ? scene.directive : {};
    const sceneState = scene && scene.state ? scene.state : null;
    const spokenText = String(turn.reply || directive.spoken_text || "").trim();
    const subtitleText = String(
      (sceneState && sceneState.subtitle_text) || spokenText || directive.subtitle_text || "",
    ).trim();
    const action = String(directive.action || "none").trim();
    const controlError = String(scene.control_error || "").trim();
    const roundStatus = String(directive.round_status || "ongoing").trim().toLowerCase() || "ongoing";
    const userText = String(settings.userText || turn.prompt || "").trim();

    if (sceneState) {
      await applySceneState(sceneState, true);
    }
    if (subtitleText) {
      elements.subtitleInput.value = subtitleText;
    }
    if (settings.updateCurrentTurn !== false && (userText || spokenText)) {
      setCurrentTurn(userText, spokenText);
    }

    if (spokenText) {
      log(`${settings.originLabel} 回复：${spokenText}`);
    }
    if (action && action !== "none") {
      if (controlError) {
        log(`${settings.originLabel} 已触发 ${action}，但机械臂联动失败：${controlError}`);
        setStatus(`${settings.originLabel} 已回复，机械臂联动失败：${controlError}`);
      } else {
        log(`${settings.originLabel} 已触发机械臂动作：${action}`);
        setStatus(settings.completedStatus);
      }
    } else {
      setStatus(settings.completedStatus);
    }

    if (settings.updateRiddle !== false) {
      if (roundStatus === "solved" || roundStatus === "revealed") {
        setActiveRiddle("", roundStatus);
      }
    }

    return {
      turn,
      scene,
      directive,
      sceneState,
      spokenText,
      subtitleText,
      action,
      controlError,
      roundStatus,
    };
  }

  async function triggerAgentTurn(message) {
    const prompt = String(message || "").trim();
    if (!prompt) {
      throw new Error("请输入要发给 agent 的问题");
    }
    if (runtime.agentTurnPending) {
      throw new Error("当前还有一轮 agent 正在处理");
    }

    setAgentBusy(true);
    setStatus("Agent 正在思考海龟汤回合...");
    log(`已发送给 agent：${prompt}`);

    try {
      const result = await requestJson("api/v1/haiguitang/agent/turn", {
        timeoutMs: AGENT_REQUEST_TIMEOUT_MS,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ message: prompt }),
      });
      await applyAgentTurnResult(result, {
        originLabel: "Agent",
        completedStatus: "Agent 已完成这一轮互动。",
        userText: prompt,
      });
      return result;
    } finally {
      setAgentBusy(false);
    }
  }

  async function ensureMotionPrimed() {
    if (runtime.motionPrimed) {
      return;
    }
    await requestJson("api/v1/haiguitang/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(MOTION_START_PAYLOAD),
    });
    runtime.motionPrimed = true;
    log("机械臂动作模板已启动。");
  }

  async function ensureRobotConnected() {
    const sessionStatus = await requestJson("api/v1/session/status");
    if (sessionStatus.connected) {
      log(`机械臂 session 已在线：mode=${sessionStatus.mode || "unknown"}`);
      return sessionStatus;
    }

    setStatus("正在连接机械臂...");
    log("检测到机器人未连接，正在调用 session/connect。");
    const connectResult = await requestJson("api/v1/session/connect", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        prefer_real: true,
        allow_sim_fallback: false,
      }),
    });

    if (!connectResult.connected) {
      throw new Error("机器人 session 仍未建立");
    }

    log(`机械臂连接成功：mode=${connectResult.mode || "unknown"}`);
    return connectResult;
  }

  async function triggerHardwareAction(action) {
    await ensureRobotConnected();
    await ensureMotionPrimed();
    await requestJson("api/v1/haiguitang/act", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ action }),
    });
    log(`机械臂动作已触发：${action}`);
    await presentSceneState({
      clip: action,
      subtitle_text: "",
      loop_playback: false,
    });
  }

  async function refreshConfigAndSceneState() {
    runtime.motionPrimed = false;
    setStatus("正在拉取场景配置...");
    runtime.config = await requestJson("api/v1/scenes/haiguitang/config");
    elements.apiHintText.textContent =
      "推荐直接从 momo_robot_service 打开这个页面。当前素材目录：" +
      String(runtime.config.media_directory_path || "未提供");

    setStatus("正在读取场景状态...");
    const sceneState = await requestJson("api/v1/scenes/haiguitang/state");
    runtime.latestSceneState = sceneState;

    if (!runtime.startupSceneHandled) {
      runtime.startupSceneHandled = true;
      if (sceneStateIsCustom(sceneState)) {
        await applySceneState(sceneState, true);
      } else {
        runtime.lastVersion = Number(sceneState.version || 0);
        await playIntro(readConfigVideoUrl("intro_video_url"));
      }
    } else {
      await applySceneState(sceneState, true);
    }
  }

  function startPolling() {
    if (runtime.pollTimer !== null) {
      window.clearInterval(runtime.pollTimer);
    }
    runtime.pollTimer = window.setInterval(async () => {
      try {
        const sceneState = await requestJson("api/v1/scenes/haiguitang/state");
        setSyncBadge("同步中", true);
        await applySceneState(sceneState, false);
      } catch (error) {
        setSyncBadge("接口离线", false);
        log(`场景轮询失败：${error.message}`);
      }
    }, POLL_INTERVAL_MS);
  }

  function setConsoleOpen(open) {
    elements.consolePanel.classList.toggle("is-open", Boolean(open));
  }

  async function enterFullscreen() {
    if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
      await document.documentElement.requestFullscreen();
      return;
    }
    if (document.fullscreenElement && document.exitFullscreen) {
      await document.exitFullscreen();
    }
  }

  function bindEvents() {
    elements.consoleToggleButton.addEventListener("click", () => {
      setConsoleOpen(true);
    });

    elements.consoleCloseButton.addEventListener("click", () => {
      setConsoleOpen(false);
    });

    elements.fullscreenButton.addEventListener("click", () => {
      enterFullscreen().catch((error) => {
        log(`沉浸模式切换失败：${error.message}`);
      });
    });

    if (elements.difficultyButton) {
      elements.difficultyButton.addEventListener("click", () => {
        if (runtime.roundStartPending) {
          return;
        }
        setModePickerOpen(true, "重新选一个难度后，Agent 会立刻开始新一局。");
      });
    }

    if (elements.audioInputSelect) {
      elements.audioInputSelect.addEventListener("change", () => {
        runtime.selectedAudioInputId = normalizeAudioInputId(elements.audioInputSelect.value);
        safeStorageSet("haiguitang_audio_input_id", runtime.selectedAudioInputId);
        renderAudioInputOptions();
        log(`已选择麦克风输入：${elements.audioInputSelect.options[elements.audioInputSelect.selectedIndex].textContent}`);
      });
    }

    if (elements.audioInputRefreshButton) {
      elements.audioInputRefreshButton.addEventListener("click", () => {
        refreshAudioInputDevices().then(() => {
          log("已刷新麦克风设备列表。");
        });
      });
    }

    elements.modeButtons.forEach((button) => {
      button.addEventListener("click", async () => {
        if (runtime.roundStartPending) {
          return;
        }
        try {
          await startRoundWithDifficulty(button.dataset.difficulty || "");
        } catch (_error) {
        }
      });
    });

    if (elements.voiceModeButton) {
      elements.voiceModeButton.addEventListener("click", async () => {
        try {
          if (runtime.roundStartPending) {
            return;
          }
          if (runtime.voicePhase === "speaking") {
            await interruptSpeakingAndStartVoiceMode();
            return;
          }
          if (runtime.voiceModeActive) {
            await disableVoiceMode();
            return;
          }
          if (!normalizeDifficulty(runtime.selectedDifficulty)) {
            setModePickerOpen(true, "先选难度，再开始单次录音。");
            setStatus("请先选择难度。");
            return;
          }
          await enableVoiceMode();
        } catch (error) {
          runtime.voiceModeActive = false;
          setVoicePhase("error");
          setStatus(`录音启动失败：${error.message}`);
          log(`录音启动失败：${error.message}`);
        }
      });
    }

    elements.saveApiButton.addEventListener("click", async () => {
      const rawInput = elements.apiBaseInput.value;
      runtime.apiBase = normalizeApiBase(elements.apiBaseInput.value);
      safeStorageSet("haiguitang_api_base", runtime.apiBase);
      elements.apiBaseInput.value = runtime.apiBase;
      runtime.lastVersion = Number.MIN_SAFE_INTEGER;
      runtime.startupSceneHandled = false;
      setSyncBadge("正在重连", true);
      log(`已更新 API Base：${runtime.apiBase}`);
      if (String(rawInput || "").includes("127.0.0.1") && !runtime.apiBase.includes("127.0.0.1")) {
        log("检测到当前页面不是 localhost，已把 127.0.0.1 自动改成当前页面地址。");
      }
      try {
        await refreshConfigAndSceneState();
        setSyncBadge("同步中", true);
        log("API 地址更新完成，场景已重新同步。");
      } catch (error) {
        setSyncBadge("接口离线", false);
        showFallback("接口未连通", `请确认 momo_robot_service 已启动。\n${error.message}`);
        log(`重新同步失败：${error.message}`);
      }
    });

    elements.reloadButton.addEventListener("click", async () => {
      runtime.lastVersion = Number.MIN_SAFE_INTEGER;
      try {
        await refreshConfigAndSceneState();
        setSyncBadge("同步中", true);
        log("已重新拉取配置和场景状态。");
      } catch (error) {
        setSyncBadge("接口离线", false);
        log(`重新同步失败：${error.message}`);
      }
    });

    if (elements.agentAskButton && elements.agentPromptInput) {
      elements.agentAskButton.addEventListener("click", async () => {
        try {
          await triggerAgentTurn(elements.agentPromptInput.value);
        } catch (error) {
          setStatus(`Agent 请求失败：${error.message}`);
          log(`Agent 请求失败：${error.message}`);
        }
      });

      elements.agentPromptInput.addEventListener("keydown", (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          elements.agentAskButton.click();
        }
      });
    }

    elements.sttStartButton.addEventListener("click", async () => {
      try {
        await startSttCapture();
      } catch (error) {
        setSttState("未连接", `实时 STT 启动失败：${error.message}`, false);
        log(`实时 STT 启动失败：${error.message}`);
      }
    });

    elements.sttStopButton.addEventListener("click", async () => {
      try {
        await stopSttCapture({ notifyServer: true, reasonLabel: "听写已停止。" });
      } catch (error) {
        finishSttSession("异常", `停止实时 STT 失败：${error.message}`, false);
        log(`停止实时 STT 失败：${error.message}`);
      }
    });

    elements.sttClearButton.addEventListener("click", () => {
      resetSttTranscript();
      log("已清空 STT 转写面板。");
    });

    elements.sttUsePromptButton.addEventListener("click", () => {
      try {
        fillTranscriptIntoAgentInput();
      } catch (error) {
        log(`填入 Agent 输入框失败：${error.message}`);
      }
    });

    elements.sceneActionButtons.forEach((button) => {
      button.addEventListener("click", async () => {
        const clip = button.dataset.clip || "default";
        const loopPlayback = button.dataset.loop === "true";
        try {
          await presentSceneState({
            clip,
            subtitle_text: "",
            loop_playback: loopPlayback,
          });
        } catch (error) {
          log(`场景切换失败：${error.message}`);
        }
      });
    });

    elements.sendSubtitleButton.addEventListener("click", async () => {
      const subtitle = elements.subtitleInput.value.trim();
      try {
        await presentSceneState({
          clip: currentClip(),
          subtitle_text: subtitle,
          loop_playback: currentLoopPlayback(),
        });
      } catch (error) {
        log(`字幕发送失败：${error.message}`);
      }
    });

    elements.clearSubtitleButton.addEventListener("click", async () => {
      elements.subtitleInput.value = "";
      try {
        await presentSceneState({
          clip: currentClip(),
          subtitle_text: "",
          loop_playback: currentLoopPlayback(),
        });
      } catch (error) {
        log(`字幕清空失败：${error.message}`);
      }
    });

    elements.hardwareNodButton.addEventListener("click", async () => {
      try {
        await triggerHardwareAction("nod");
      } catch (error) {
        runtime.motionPrimed = false;
        setStatus(`机械臂点头失败：${error.message}`);
        log(`机械臂点头失败：${error.message}`);
      }
    });

    elements.hardwareShakeButton.addEventListener("click", async () => {
      try {
        await triggerHardwareAction("shake");
      } catch (error) {
        runtime.motionPrimed = false;
        setStatus(`机械臂摇头失败：${error.message}`);
        log(`机械臂摇头失败：${error.message}`);
      }
    });

    elements.clearLogButton.addEventListener("click", () => {
      elements.logOutput.textContent = "Ready.";
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        setConsoleOpen(false);
      }
    });

    if (navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function") {
      navigator.mediaDevices.addEventListener("devicechange", () => {
        refreshAudioInputDevices();
      });
    }
  }

  async function bootstrap() {
    runtime.apiBase = readInitialApiBase();
    runtime.selectedAudioInputId = normalizeAudioInputId(safeStorageGet("haiguitang_audio_input_id") || "default");
    try {
      const storedRiddle = safeStorageGet("haiguitang_active_riddle");
      const parsedRiddle = storedRiddle ? JSON.parse(storedRiddle) : null;
      runtime.activeRiddleText = String(parsedRiddle && parsedRiddle.text || "").trim();
      runtime.activeRiddleStatus = String(parsedRiddle && parsedRiddle.status || "idle").trim().toLowerCase() || "idle";
    } catch (_error) {
      runtime.activeRiddleText = "";
      runtime.activeRiddleStatus = "idle";
    }
    try {
      const storedTurn = safeStorageGet("haiguitang_current_turn");
      const parsedTurn = storedTurn ? JSON.parse(storedTurn) : null;
      runtime.currentTurnUserText = String(parsedTurn && parsedTurn.user || "").trim();
      runtime.currentTurnAgentText = String(parsedTurn && parsedTurn.agent || "").trim();
    } catch (_error) {
      runtime.currentTurnUserText = "";
      runtime.currentTurnAgentText = "";
    }
    elements.apiBaseInput.value = runtime.apiBase;
    setStatus("正在连接场景接口...");
    setSyncBadge("正在连接", true);
    resetSttTranscript();
    setSttState("未连接", "等待连接 AWS 实时 STT。", false);
    renderActiveRiddle();
    renderCurrentTurn();
    renderAudioInputOptions();
    setSelectedDifficulty("");
    setModePickerOpen(true, "先选一个难度，进入后会由 Agent 先说题面。");
    setVoicePhase("idle");
    bindEvents();
    refreshAudioInputDevices();

    const currentUrl = currentOriginUrl();
    if (
      currentUrl &&
      !isLoopbackHost(currentUrl.hostname) &&
      isLoopbackHost(normalizeCandidateUrl(runtime.apiBase)?.hostname)
    ) {
      runtime.apiBase = defaultApiBase();
      elements.apiBaseInput.value = runtime.apiBase;
      safeStorageSet("haiguitang_api_base", runtime.apiBase);
      log(`检测到错误的本地回环地址缓存，已自动改回当前页面地址：${runtime.apiBase}`);
    }

    try {
      await refreshConfigAndSceneState();
      try {
        const sttStatus = await requestJson("api/v1/stt/aws/status");
        const available = Boolean(sttStatus.available);
        const region = String(sttStatus.region || "unknown").trim();
        const sampleRate = Number(sttStatus.sample_rate_hz || STT_TARGET_SAMPLE_RATE);
        const encoding = String(sttStatus.media_encoding || "pcm").trim();
        if (available) {
          setSttState("待命", `AWS 实时 STT 可用，region=${region}，默认音频=${sampleRate}Hz/${encoding}。`, false);
        } else {
          setSttState(
            "不可用",
            `AWS 实时 STT 依赖未就绪：${String(sttStatus.import_error || "unknown error")}`,
            false,
          );
        }
      } catch (error) {
        setSttState("未知", `无法读取 STT 状态：${error.message}`, false);
      }
      setSyncBadge("同步中", true);
      startPolling();
      log(`页面已就绪，当前接口：${runtime.apiBase}`);
    } catch (error) {
      setSyncBadge("接口离线", false);
      showFallback("接口未连通", `请确认 momo_robot_service 已启动。\n${error.message}`);
      log(`初始化失败：${error.message}`);
    }
  }

  try {
    bootstrap();
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    setStatus("前端启动失败");
    setSyncBadge("前端异常", false);
    showFallback("页面启动失败", `前端脚本启动时报错。\n${message}`);
    log(`前端启动失败：${message}`);
  }
})();
