(function () {
  const POLL_INTERVAL_MS = 800;
  const REQUEST_TIMEOUT_MS = 5000;
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
    apiBaseInput: document.getElementById("apiBaseInput"),
    apiHintText: document.getElementById("apiHintText"),
    reloadButton: document.getElementById("reloadButton"),
    saveApiButton: document.getElementById("saveApiButton"),
    agentPromptInput: document.getElementById("agentPromptInput"),
    agentAskButton: document.getElementById("agentAskButton"),
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
    sceneActionButtons: Array.from(document.querySelectorAll(".scene-action")),
  };

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
  };

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
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = controller
      ? window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
      : null;
    try {
      const response = await fetch(resolveApiUrl(path), {
        method: "GET",
        headers: {
          Accept: "application/json",
        },
        cache: "no-store",
        signal: controller ? controller.signal : undefined,
        ...options,
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
    elements.sceneVideo.loop = Boolean(settings.loop);
    elements.sceneVideo.muted = true;
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

    elements.sceneVideo.onerror = () => {
      if (token !== runtime.playToken) {
        return;
      }
      showFallback(settings.fallbackTitle, settings.fallbackBody);
      log(`视频加载失败：${url}`);
    };

    try {
      await elements.sceneVideo.play();
    } catch (error) {
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
      fallbackBody: "没有检测到 default.mp4。请把默认表情视频放到 runtime/media 目录里。",
    });
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
      fallbackBody: "没有检测到 begin.mp4，已经回退到默认角色视频。",
      onEnded: () => {
        playDefaultLoop(runtime.latestSceneState);
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
        fallbackBody: "没有检测到 nod.mp4，已经回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
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
        fallbackBody: "没有检测到 shake.mp4，已经回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
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
        fallbackBody: "没有检测到 end.mp4，已经回退到默认角色视频。",
        onEnded: () => {
          if (!shouldLoop) {
            playDefaultLoop(sceneState);
          }
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

  async function triggerAgentTurn(message) {
    const prompt = String(message || "").trim();
    if (!prompt) {
      throw new Error("请输入要发给 agent 的问题");
    }
    if (runtime.agentTurnPending) {
      throw new Error("当前还有一轮 agent 正在处理");
    }

    setAgentBusy(true);
    setStatus("OpenClaw 正在思考海龟汤回合...");
    log(`已发送给 agent：${prompt}`);

    try {
      const result = await requestJson("api/v1/haiguitang/agent/turn", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ message: prompt }),
      });

      const turn = result && result.turn ? result.turn : {};
      const scene = result && result.scene ? result.scene : {};
      const directive = scene && scene.directive ? scene.directive : {};
      const sceneState = scene && scene.state ? scene.state : null;
      const spokenText = String(turn.reply || directive.spoken_text || "").trim();
      const subtitleText = String(directive.subtitle_text || "").trim();
      const action = String(directive.action || "none").trim();
      const controlError = String(scene.control_error || "").trim();

      if (sceneState) {
        await applySceneState(sceneState, true);
      }
      if (subtitleText) {
        elements.subtitleInput.value = subtitleText;
      }

      if (spokenText) {
        log(`Agent 回复：${spokenText}`);
      }
      if (action && action !== "none") {
        if (controlError) {
          log(`Agent 已触发 ${action}，但机械臂联动失败：${controlError}`);
          setStatus(`Agent 已回复，机械臂联动失败：${controlError}`);
        } else {
          log(`Agent 已触发机械臂动作：${action}`);
          setStatus("Agent 已完成这一轮互动。");
        }
      } else {
        setStatus("Agent 已完成这一轮互动。");
      }

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
      "推荐直接从 quick_control_api 打开这个页面。当前素材目录：" +
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
        showFallback("接口未连通", `请确认 quick_control_api 已启动。\n${error.message}`);
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
  }

  async function bootstrap() {
    runtime.apiBase = readInitialApiBase();
    elements.apiBaseInput.value = runtime.apiBase;
    setStatus("正在连接场景接口...");
    setSyncBadge("正在连接", true);
    bindEvents();

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
      setSyncBadge("同步中", true);
      startPolling();
      log(`页面已就绪，当前接口：${runtime.apiBase}`);
    } catch (error) {
      setSyncBadge("接口离线", false);
      showFallback("接口未连通", `请确认 quick_control_api 已启动。\n${error.message}`);
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
