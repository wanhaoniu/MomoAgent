# Quick Control API Frontend Integration

这份文档给前端 / App 同事使用，重点只覆盖当前已经确定的两部分：

- `agent` 文本交互
- `tts` 语音播放

当前推荐边界：

- 前端负责 UI、录音、STT、播放音频、展示状态
- 控制器后端负责和 OpenClaw agent 交互
- 如果需要 TTS，前端仍然只连控制器后端；由控制器后端再去桥接远端流式 TTS 服务
- `follow` / `idle_scan` 属于机械臂行为层，不是本文件重点

## 1. Base URL

HTTP:

- `http://<controller-ip>:8010`

WebSocket:

- `ws://<controller-ip>:8010`

本地联调示例：

- HTTP: `http://127.0.0.1:8010`
- WS: `ws://127.0.0.1:8010`

## 2. 前端最推荐的接法

### 推荐结论

如果前端要做“对话 + 语音播报”，优先使用：

- `WS /api/v1/ws/agent-stream`

这是当前最完整的单入口。它同时覆盖：

- 发起一轮 agent 对话
- 收到文本回复
- 可选触发 TTS
- 收到流式音频块
- 收到本轮结束状态

### 什么时候还需要 REST

前端通常只需要额外补这几个 REST：

- `GET /api/v1/health`
- `GET /api/v1/agent/status`
- `GET /api/v1/agent/last-turn`
- `POST /api/v1/agent/warmup`
- `POST /api/v1/agent/reset-session`

简单理解：

- WebSocket 负责“进行中的一轮”
- REST 负责“初始化、状态恢复、页面重进后的兜底”

## 3. Agent 相关接口

### 3.1 `GET /api/v1/health`

用途：

- 判断控制器服务是否在线
- 顺便拿到 `session` 和 `agent` 概况

返回结构重点：

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "service": "momoagent-quick-control-api",
    "session": {},
    "agent": {}
  }
}
```

### 3.2 `GET /api/v1/agent/status`

用途：

- 页面初始化时读取 agent 当前状态
- 判断当前是否 `busy`
- 判断后端 TTS 是否可用

关键字段：

- `data.enabled`: agent 功能是否启用
- `data.busy`: 是否正在处理上一轮
- `data.session_id`: 当前 OpenClaw session id
- `data.bridge_session_key`: bridge session key
- `data.last_error`: 最近一次 agent 错误
- `data.tts.enabled`: 是否开启 TTS 功能
- `data.tts.available`: 当前是否可用
- `data.tts.base_url`: 远端 TTS 服务地址
- `data.tts.last_error`: 最近一次 TTS 健康检查错误
- `data.last_turn`: 最近一轮的记录

### 3.3 `GET /api/v1/agent/last-turn`

用途：

- 页面刷新后恢复“上一轮对话结果”
- 调试时查看最后一轮的文本 / TTS 摘要

关键字段：

- `kind`: `ask` / `warmup` / `reset_session`
- `status`: `ok` / `error` / `idle`
- `prompt`: 用户输入
- `reply`: agent 文本输出
- `error`: 错误信息
- `session_id`
- `bridge_session_key`
- `openclaw_elapsed_sec`
- `bridge_timing`
- `tts`
- `updated_at`

### 3.4 `POST /api/v1/agent/warmup`

用途：

- 建议 App 启动后调用一次，用来热启动 OpenClaw session
- 减少首轮冷启动延迟

请求体：

```json
{
  "prompt": "请只回复“就绪”。"
}
```

说明：

- 不传也可以，后端有默认值
- 前端通常只在页面初始化或连接完成后调用一次

### 3.5 `POST /api/v1/agent/reset-session`

用途：

- 用户主动“新开对话”
- 清掉当前 OpenClaw session 上下文

无请求体。

### 3.6 `POST /api/v1/agent/ask`

用途：

- 文本模式的一次性请求
- 只拿最终文本结果，不拿流式 TTS 音频

请求体：

```json
{
  "message": "你好，请介绍一下你自己。"
}
```

适用场景：

- 纯文本聊天
- 调试 agent 是否可用
- 不需要语音播报时

如果前端需要 TTS，请优先使用 `WS /api/v1/ws/agent-stream`，不要先 `POST /agent/ask` 再自己拼第二套 TTS 调用链。

## 4. 流式 Agent + TTS

### 4.1 `WS /api/v1/ws/agent-stream`

用途：

- 当前前端对接的主入口
- 一次 WebSocket 连接上可以反复发送多轮 `ask`

连接成功后，服务端会先主动发送：

```json
{
  "type": "ready",
  "data": {
    "status": {}
  }
}
```

### 4.2 客户端发什么

#### 心跳

客户端可以发送：

```json
{
  "type": "ping"
}
```

服务端返回：

```json
{
  "type": "pong"
}
```

#### 主动取状态

客户端可以发送：

```json
{
  "type": "status"
}
```

服务端返回：

```json
{
  "type": "status",
  "data": {}
}
```

#### 发起一轮对话

文本模式：

```json
{
  "type": "ask",
  "message": "你好",
  "with_tts": false
}
```

文本 + TTS 模式：

```json
{
  "type": "ask",
  "message": "你好，和我打个招呼。",
  "with_tts": true
}
```

字段说明：

- `type`: 固定为 `ask`
- `message`: 用户文本
- `with_tts`: 是否让后端在拿到 agent 文本回复后继续触发流式 TTS

## 5. WebSocket 事件语义

下面是前端最需要处理的事件。

### 5.1 `turn_started`

表示这一轮已被接受。

```json
{
  "type": "turn_started",
  "with_tts": true,
  "message": "你好"
}
```

### 5.2 `agent_reply`

表示 agent 文本结果已经出来。

```json
{
  "type": "agent_reply",
  "data": {
    "kind": "ask",
    "status": "ok",
    "prompt": "你好",
    "reply": "你好呀！今天有什么我可以帮你的吗？",
    "error": "",
    "session_id": "...",
    "bridge_session_key": "...",
    "openclaw_elapsed_sec": 1.03,
    "bridge_timing": {
      "accept_ms": 120.0,
      "wait_ms": 890.0,
      "history_ms": 30.0,
      "total_ms": 1030.0
    },
    "tts": {
      "requested": false
    },
    "updated_at": 1775391766.0
  }
}
```

说明：

- 如果 `with_tts=false`，前端拿到这个事件后通常就可以直接展示文本，等待后面的 `turn_done`
- 如果 `with_tts=true`，这只是文本先返回，后面还会继续收到 TTS 相关事件

### 5.3 `tts_started`

表示后端确认本轮准备开始桥接远端 TTS。

```json
{
  "type": "tts_started",
  "data": {
    "requested": true,
    "ok": false,
    "base_url": "http://192.168.66.92:7999",
    "model": "qwen/qwen3.5-35b-a3b",
    "input_chars": 18,
    "error": ""
  }
}
```

注意：

- 这里的 `ok` 还不是“已经播完”，只是 TTS 请求已准备开始

### 5.4 `tts_unavailable`

表示用户请求了 TTS，但当前后端判断 TTS 不可用。

```json
{
  "type": "tts_unavailable",
  "data": {
    "requested": true,
    "ok": false,
    "error": "Remote TTS bridge is unavailable ..."
  }
}
```

前端建议：

- 文本照常展示
- UI 上提示“语音暂不可用”
- 不要把整轮 agent 视为失败

### 5.5 `tts_session_ready`

表示远端 TTS WebSocket session 已建立。

```json
{
  "type": "tts_session_ready",
  "session_id": "abc123",
  "model": "qwen/qwen3.5-35b-a3b",
  "sample_rate": 44100,
  "interrupt_path": "/api/v1/sessions/abc123/interrupt"
}
```

说明：

- 目前前端不需要直接调用这个 `interrupt_path`
- 这个字段主要用于调试和后续扩展

### 5.6 `tts_llm_delta`

表示远端 TTS 服务内部的增量文本。

```json
{
  "type": "tts_llm_delta",
  "content": "你好"
}
```

前端建议：

- 可以忽略
- 不建议在 UI 上逐字显示它，因为会比较碎

### 5.7 `tts_segment`

表示远端 TTS 正在处理的一段文本。

```json
{
  "type": "tts_segment",
  "text": "你好，很高兴见到你。"
}
```

前端建议：

- 可选展示
- 更适合做调试，不一定要暴露给普通用户

### 5.8 `tts_segment_done`

表示一个 TTS 文本分段已经合成完成。

```json
{
  "type": "tts_segment_done",
  "elapsed_seconds": 0.284,
  "total_samples": 96320
}
```

### 5.9 `audio_chunk`

表示一段音频数据到达。这是前端真正需要播放的核心事件。

```json
{
  "type": "audio_chunk",
  "pcm16_base64": "...",
  "sample_rate": 44100
}
```

字段说明：

- `pcm16_base64`: base64 编码后的单声道 PCM16 数据
- `sample_rate`: 当前块对应的采样率

前端需要做的事：

1. base64 解码
2. 按 `Int16` 解释 PCM 数据
3. 转成 Web Audio 可播放的浮点采样
4. 做一个小缓冲队列后顺序播放

说明：

- 目前是流式块，不是完整 mp3 / wav 文件
- 不能直接把 `pcm16_base64` 当作音频 URL 使用

### 5.10 `tts_warning`

表示远端 TTS 在已经有部分音频产出后，遇到非致命警告。

```json
{
  "type": "tts_warning",
  "message": "..."
}
```

前端建议：

- 记录日志即可
- 不一定要中断本地播放

### 5.11 `interrupted`

表示远端 TTS 会话被中断。

```json
{
  "type": "interrupted",
  "reason": "..."
}
```

目前前端可以先把它视为“本轮 TTS 提前结束”。

### 5.12 `turn_done`

表示这一轮彻底结束，是前端最重要的结束事件。

```json
{
  "type": "turn_done",
  "data": {
    "turn": {
      "kind": "ask",
      "status": "ok",
      "prompt": "你好",
      "reply": "你好呀！今天有什么我可以帮你的吗？",
      "tts": {
        "requested": true,
        "ok": true,
        "session_id": "abc123",
        "spoken_text": "你好呀！今天有什么我可以帮你的吗？",
        "sample_rate": 44100,
        "audio_chunks": 26,
        "audio_bytes": 135680,
        "finish_reason": "remote_done",
        "elapsed_sec": 2.13,
        "error": ""
      }
    },
    "status": {}
  }
}
```

前端建议：

- 把这一轮消息标记为完成
- 用 `turn.tts` 更新播放摘要 / 调试信息
- 用 `status` 刷新页面上的 agent 状态

### 5.13 `error`

表示 WebSocket 请求级错误。

```json
{
  "type": "error",
  "stage": "agent",
  "code": "AGENT_FAILED",
  "message": "..."
}
```

字段说明：

- `stage`: 常见为 `request` / `agent` / `tts`
- `code`: 错误码
- `message`: 错误说明

## 6. 推荐的前端交互流程

### 6.1 页面初始化

1. 调 `GET /api/v1/health`
2. 调 `GET /api/v1/agent/status`
3. 可选调一次 `POST /api/v1/agent/warmup`
4. 建立 `WS /api/v1/ws/agent-stream`

### 6.2 用户发送一条消息

1. 前端完成 STT，拿到文本
2. 通过 WebSocket 发送：

```json
{
  "type": "ask",
  "message": "<用户文本>",
  "with_tts": true
}
```

3. 收到 `agent_reply` 后先显示文本
4. 收到 `audio_chunk` 后开始流式播放
5. 收到 `turn_done` 后把这轮标记结束

### 6.3 页面刷新 / 重连

1. 重新建立 WebSocket
2. 调 `GET /api/v1/agent/last-turn`
3. 用最近一轮结果恢复 UI

## 7. 音频播放实现建议

### 7.1 推荐方案

前端使用 Web Audio API 自己维护一个 PCM 播放队列。

核心原因：

- 后端返回的是流式 `PCM16`
- 不是一个完整文件
- 如果每块到了就立刻播，容易卡顿
- 做一个 100ms 到 300ms 左右的小缓冲会更稳

### 7.2 数据处理方式

每个 `audio_chunk`：

1. `atob()` 或等价方法解 base64
2. 按 little-endian `Int16` 读取
3. 归一化成 `[-1, 1]` 浮点
4. 写入 `AudioBuffer`
5. 串行调度播放时间

### 7.3 前端要注意

- 收到第一块音频时再真正开始调度，通常更稳
- 不建议把 `tts_llm_delta` 当逐字字幕显示
- 如果用户点“停止播放”，当前版本先停止本地播放即可
- 当前版本没有专门对前端暴露一个“停止远端 TTS”接口，所以先不要依赖远端中断能力做主交互

## 8. 错误格式

### REST 错误

```json
{
  "ok": false,
  "error": {
    "code": "NOT_CONNECTED",
    "message": "Robot is not connected"
  }
}
```

### WebSocket 错误

```json
{
  "type": "error",
  "stage": "request",
  "code": "INVALID_ARGUMENT",
  "message": "Agent prompt is empty"
}
```

## 9. 给前端同事的最短结论

前端如果只关心“聊天 + 播放语音”，请按下面接：

- 初始化：
  - `GET /api/v1/health`
  - `GET /api/v1/agent/status`
  - 可选 `POST /api/v1/agent/warmup`
- 主通道：
  - `WS /api/v1/ws/agent-stream`
- 恢复上一轮：
  - `GET /api/v1/agent/last-turn`
- 新开对话：
  - `POST /api/v1/agent/reset-session`

最重要的一点：

- 前端不要自己再去直连另一套 TTS 服务
- 只需要向控制器后端发 `with_tts=true`
- 然后在同一个 WebSocket 里接收 `audio_chunk` 并播放
