# Realtime STT App 接入文档

这份文档给需要在 App 端接入 MomoAgent 实时语音转文字能力的同学使用。

当前链路已经跑通，整体结构是：

1. App 采集麦克风音频
2. App 把音频按 `WebSocket` 二进制帧发给 `momo_robot_service`
3. `momo_robot_service` 在服务端调用 AWS Transcribe Streaming
4. 后端把 `partial / final` 转写结果实时回推给 App

这样做的好处是：

- App 不需要持有 AWS 密钥
- AWS 权限只保留在服务端
- App 只需要会录音、开 WebSocket、收 JSON

## 1. 接口概览

服务默认由 `momo_robot_service` 提供。

基础地址示例：

```text
http://<host>:8010/
ws://<host>:8010/
```

当前实时 STT 相关接口：

- `GET /api/v1/stt/aws/status`
- `WS /api/v1/ws/stt`

## 2. 服务端职责

服务端实现位置：

- `Software/Master/momo_robot_service/src/momo_robot_service/aws_transcribe_realtime.py`
- `Software/Master/momo_robot_service/src/momo_robot_service/app.py`

服务端负责：

- 读取 AWS 凭证与区域
- 建立 AWS Transcribe Streaming 会话
- 校验客户端的启动参数
- 把客户端上传的 PCM 音频转发到 AWS
- 把 AWS 回来的 partial / final 文本转换成统一 JSON

## 3. 启动前检查

建议先用状态接口确认后端是否就绪：

```http
GET /api/v1/stt/aws/status
```

典型返回：

```json
{
  "ok": true,
  "data": {
    "available": true,
    "region": "us-east-2",
    "language_code": "zh-CN",
    "media_encoding": "pcm",
    "sample_rate_hz": 16000,
    "partial_results_stability": "medium",
    "max_audio_chunk_bytes": 32768,
    "import_error": ""
  }
}
```

如果 `available=false`，通常是：

- 服务端没有安装 `amazon-transcribe`
- AWS 凭证没有配置好
- 区域或运行环境不正确

## 4. WebSocket 协议

连接地址：

```text
ws://<host>:8010/api/v1/ws/stt
```

协议分两类消息：

- 文本帧：JSON 控制消息
- 二进制帧：原始音频数据

### 4.1 建连后服务端首条消息

客户端连上后，服务端会先发一个 `ready`：

```json
{
  "type": "ready",
  "data": {
    "state": "idle",
    "config": {
      "available": true,
      "region": "us-east-2",
      "language_code": "zh-CN",
      "media_encoding": "pcm",
      "sample_rate_hz": 16000,
      "partial_results_stability": "medium",
      "max_audio_chunk_bytes": 32768,
      "import_error": ""
    },
    "expected_audio": {
      "mediaEncoding": "pcm",
      "sampleRateHertz": 16000,
      "channels": 1,
      "chunkDurationMs": 100
    }
  }
}
```

### 4.2 客户端启动识别

收到 `ready` 后，客户端发送：

```json
{
  "type": "start",
  "languageCode": "zh-CN",
  "mediaEncoding": "pcm",
  "sampleRateHertz": 16000,
  "partialResultsStability": "medium"
}
```

字段说明：

- `languageCode`
  - 当前推荐：`zh-CN`
- `mediaEncoding`
  - 当前推荐：`pcm`
- `sampleRateHertz`
  - 当前推荐：`16000`
- `partialResultsStability`
  - 可选：`low` / `medium` / `high`
  - 当前推荐：`medium`

### 4.3 服务端确认流已启动

成功后服务端会返回：

```json
{
  "type": "stream_started",
  "data": {
    "sessionId": "xxxx",
    "region": "us-east-2",
    "languageCode": "zh-CN",
    "mediaEncoding": "pcm",
    "sampleRateHertz": 16000,
    "partialResultsStability": "medium"
  }
}
```

### 4.4 客户端持续发送音频

从这一步开始，客户端持续发送二进制音频帧。

要求：

- 单声道
- `PCM16 little-endian`
- 推荐采样率：`16000Hz`
- 推荐切片时长：`100ms`
- 单个二进制帧建议不要超过 `32KB`

也就是说，推荐上传的是：

```text
16kHz / mono / PCM16 / little-endian
```

### 4.5 服务端返回 partial / final

识别过程中，服务端会不断返回：

#### partial

```json
{
  "type": "partial",
  "data": {
    "sessionId": "xxxx",
    "resultId": "xxxx",
    "isPartial": true,
    "text": "我觉得这个事情",
    "startTime": 0.52,
    "endTime": 1.76,
    "channelId": "",
    "languageCode": "zh-CN",
    "stableItemCount": 4,
    "items": []
  }
}
```

#### final

```json
{
  "type": "final",
  "data": {
    "sessionId": "xxxx",
    "resultId": "xxxx",
    "isPartial": false,
    "text": "我觉得这个事情和妈妈有关。",
    "startTime": 0.52,
    "endTime": 2.41,
    "channelId": "",
    "languageCode": "zh-CN",
    "stableItemCount": 8,
    "items": []
  }
}
```

推荐客户端行为：

- `partial` 只用于实时显示
- `final` 才用于真正提交给 Agent 或写入聊天记录
- 用 `resultId` 去重，避免重复插入

### 4.6 停止识别

客户端发送：

```json
{
  "type": "stop"
}
```

服务端会返回：

```json
{
  "type": "stream_stopped",
  "data": {
    "sessionId": "xxxx",
    "state": "stopped",
    "reason": "client_stop",
    "finalSegments": 3,
    "partialSegments": 12,
    "audioBytes": 38400,
    "lastError": ""
  }
}
```

## 5. 其他控制消息

### ping

客户端：

```json
{ "type": "ping" }
```

服务端：

```json
{ "type": "pong" }
```

### status

客户端：

```json
{ "type": "status" }
```

服务端：

```json
{
  "type": "status",
  "data": {
    "state": "streaming",
    "config": {
      "available": true,
      "region": "us-east-2",
      "language_code": "zh-CN",
      "media_encoding": "pcm",
      "sample_rate_hz": 16000,
      "partial_results_stability": "medium",
      "max_audio_chunk_bytes": 32768,
      "import_error": ""
    }
  }
}
```

## 6. 错误返回

服务端统一错误格式：

```json
{
  "type": "error",
  "stage": "start",
  "code": "STT_START_FAILED",
  "message": "具体错误信息"
}
```

常见错误：

- `INVALID_JSON`
- `INVALID_MESSAGE`
- `UNSUPPORTED_OP`
- `STT_START_FAILED`
- `STT_STOP_FAILED`
- `AUDIO_SEND_FAILED`
- `TRANSCRIBE_STREAM_FAILED`

推荐客户端处理方式：

- UI 上提示用户
- 当前录音状态置为失败或停止
- 允许用户重新开始

## 7. 推荐接入流程

推荐 App 端完整流程：

1. 调 `GET /api/v1/stt/aws/status` 检查后端是否可用
2. 打开 `WS /api/v1/ws/stt`
3. 等待 `ready`
4. 发送 `start`
5. 开始录音并按 100ms 左右切片上传二进制 PCM
6. 用 `partial` 更新“正在识别”
7. 用 `final` 更新“最终识别结果”
8. 用户说完后发送 `stop`
9. 收到 `stream_stopped` 后关闭本轮会话

## 8. 音频采集要求

如果 App 端能直接录 `16kHz mono PCM16`，是最省事的。

如果系统 API 默认给的是：

- 44.1kHz
- 48kHz
- 双声道
- Float32

那就需要在客户端先做：

- 下采样到 `16000Hz`
- 混成单声道
- 转成 `PCM16 little-endian`

## 9. 输入设备选择说明

这一点非常重要。

后端 **不负责** 选择 AirPods / EarPods / 内建麦克风，输入设备始终由客户端系统决定。

也就是说：

- Web 页面如果不做设备选择，只会用“浏览器当前默认输入设备”
- App 如果不做设备选择，只会用“系统当前默认输入设备”

如果你希望 App 明确使用 AirPods / EarPods：

- iOS / Android 端需要自己做输入路由选择
- 录音前要明确指定音频输入设备
- 如果设备断开，需要自动回退到默认麦克风

我们现在的 Web 示例已经补了一个显式麦克风选择器，可以参考：

- `Software/Web/index.html`
- `Software/Web/app.js`

## 10. 最小客户端伪代码

下面是一个通用伪代码：

```text
connect ws://<host>:8010/api/v1/ws/stt
wait until receive {"type":"ready"}
send {"type":"start","languageCode":"zh-CN","mediaEncoding":"pcm","sampleRateHertz":16000,"partialResultsStability":"medium"}

start microphone capture
loop every ~100ms:
  pcm16_bytes = next_audio_chunk()
  websocket.send(binary pcm16_bytes)

on websocket message:
  if type == "partial":
    update_live_text(data.text)
  if type == "final":
    append_final_text(data.resultId, data.text)
  if type == "error":
    show_error(message)
    stop_capture()
  if type == "stream_stopped":
    stop_capture()
    close_socket()
```

## 11. JavaScript 示例

如果 App 端同学先想在桌面或 H5 上做联调，可以参考这个最小流程：

```js
const ws = new WebSocket("ws://127.0.0.1:8010/api/v1/ws/stt");
ws.binaryType = "arraybuffer";

ws.addEventListener("message", (event) => {
  const payload = JSON.parse(String(event.data || "{}"));
  if (payload.type === "ready") {
    ws.send(JSON.stringify({
      type: "start",
      languageCode: "zh-CN",
      mediaEncoding: "pcm",
      sampleRateHertz: 16000,
      partialResultsStability: "medium",
    }));
  }
  if (payload.type === "partial") {
    console.log("partial:", payload.data.text);
  }
  if (payload.type === "final") {
    console.log("final:", payload.data.text);
  }
  if (payload.type === "error") {
    console.error(payload);
  }
});

// 后续把 16kHz mono pcm16 little-endian 的音频片段用 ws.send(bytes) 发送即可
```

## 12. 和 Agent 联动的推荐方式

如果 App 想做“说一句 -> 送给 Agent -> Agent 回复”的语音交互，推荐这样接：

1. STT `final` 出一条完整句子
2. App 端把这条 final 文本提交到：
   - `POST /api/v1/haiguitang/agent/turn`
   - 或其他 Agent 接口
3. Agent 回复文本后，再决定是否调用 TTS

不要把 `partial` 直接提交给 Agent，否则会造成：

- 文本抖动
- 重复提问
- 话还没说完就触发回复

## 13. 已验证现状

当前仓库内已经完成并验证：

- AWS 实时 STT 状态检查接口
- `WS /api/v1/ws/stt`
- 浏览器端麦克风采集、下采样、PCM 上传
- `partial / final` 实时显示
- 题面固定显示与 Agent 对话联动

参考实现：

- `Software/Master/momo_robot_service/src/momo_robot_service/aws_transcribe_realtime.py`
- `Software/Master/momo_robot_service/src/momo_robot_service/app.py`
- `Software/Web/app.js`

## 14. 联调建议

如果 App 同学要开始接，建议按这个顺序：

1. 先打 `GET /api/v1/stt/aws/status`
2. 再做一个最小 WebSocket client，只收发 `ready/start/partial/final/stop`
3. 确认实时转写没问题后，再接 Agent
4. 最后再做输入设备选择、自动静音检测、TTS 联动等体验层逻辑

如果只想验证服务端是否可用，最简单的检查方式是：

- 后端启动后访问 `GET /api/v1/stt/aws/status`
- 再用任何 WebSocket 客户端连接 `WS /api/v1/ws/stt`

