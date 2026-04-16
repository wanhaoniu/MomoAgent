# Quick Control API Frontend Integration

这份文档给前端 / App 同事使用，重点只讲当前推荐方案：

- 聊天主链路使用 `WS /api/v1/ws/agent-stream`
- 页面内保持长连接，不要每问一句就断开重连
- 文本展示优先消费 `agent_delta`
- 如需语音播报，继续走同一条 WebSocket，不要再直连另一套 TTS 服务

这份文档适合直接转给前端同事落地。

## 1. 结论先说

如果 App 要做接近网页直问的体验，主流程请使用：

- `WS /api/v1/ws/agent-stream`

不推荐把下面这个接口当聊天主入口：

- `POST /api/v1/agent/ask`

原因很简单：

- `POST /api/v1/agent/ask` 是“等最终结果再返回”
- `WS /api/v1/ws/agent-stream` 能在生成过程中收到 `agent_delta`
- 当前后端已经把 OpenClaw 会话对齐到主会话，并且把内部 bridge 改成常驻，连续多轮时延会明显更稳定

如果前端继续使用单次 HTTP ask，就算后端已经修好了，会话体验还是会比网页慢。

## 2. Base URL

HTTP:

- `http://<controller-ip>:8010`

WebSocket:

- `ws://<controller-ip>:8010`

本地联调示例：

- HTTP: `http://127.0.0.1:8010`
- WS: `ws://127.0.0.1:8010`

## 3. 推荐接入方式

### 3.1 主流程

页面进入聊天态后，前端应：

1. 调一次 `GET /api/v1/health`
2. 调一次 `GET /api/v1/agent/status`
3. 建立 `WS /api/v1/ws/agent-stream`
4. 等服务端返回 `ready`
5. 用户后续所有问答都走这条 WebSocket

### 3.2 为什么要保持长连接

当前链路里有两层“热起来之后会更快”的状态：

- OpenClaw 主会话
- quick_control_api 内部到 OpenClaw gateway 的 bridge 长连接

所以前端要尽量：

- 进入聊天页时就连上 WebSocket
- 在整个聊天页生命周期里保持连接
- 不要每问一条消息就重新创建连接

### 3.3 什么时候还用 REST

REST 主要用来做状态、恢复和调试，不建议承担主聊天流量：

- `GET /api/v1/health`
- `GET /api/v1/agent/status`
- `GET /api/v1/agent/last-turn`
- `POST /api/v1/agent/reset-session`
- `POST /api/v1/agent/warmup`

`POST /api/v1/agent/ask` 建议只用于：

- 后台调试
- 非聊天型一次性请求
- 不需要流式展示的场景

## 4. 页面推荐流程

### 4.1 进入聊天页

推荐顺序：

1. `GET /api/v1/health`
2. `GET /api/v1/agent/status`
3. 建立 `WS /api/v1/ws/agent-stream`
4. 收到 `ready` 后，把输入框置为可发送
5. 可选调用 `GET /api/v1/agent/last-turn` 恢复上一轮结果

说明：

- `warmup` 不是必须
- 如果非常在意“服务刚重启后第一问”的冷启动，可以在进入页面后补一次 `POST /api/v1/agent/warmup`
- 但主流程依然应该是 WebSocket，不要回退成 HTTP ask

### 4.2 用户发送一条消息

前端本地先做 UI 动作：

1. 立即插入 user bubble
2. 立即创建一个空的 assistant bubble
3. 进入 loading / thinking 状态
4. 禁止并发发送下一条消息

然后通过 WebSocket 发：

```json
{
  "type": "ask",
  "message": "你好",
  "with_tts": false
}
```

如果要播报语音：

```json
{
  "type": "ask",
  "message": "你好，和我打个招呼。",
  "with_tts": true
}
```

### 4.3 一轮结束

以下任一情况视为本轮结束：

- 收到 `turn_done`
- 收到 `error`

结束后前端应：

- 关闭 loading
- 恢复发送按钮
- 更新当前会话状态

## 5. WebSocket 协议

主入口：

- `WS /api/v1/ws/agent-stream`

连接成功后，服务端会先发一条：

```json
{
  "type": "ready",
  "data": {
    "status": {}
  }
}
```

### 5.1 客户端可发送消息

#### 心跳

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

```json
{
  "type": "ask",
  "message": "<用户文本>",
  "with_tts": true
}
```

字段说明：

- `type`: 固定为 `ask`
- `message`: 用户文本
- `with_tts`: 是否让后端在文本回复后继续桥接 TTS

## 6. 前端必须处理的事件

下面这几个事件是聊天主流程一定要接的。

### 6.1 `ready`

表示当前 WebSocket 已可用。

前端动作：

- 标记连接成功
- 允许发送第一条消息

### 6.2 `turn_started`

表示这一轮请求已经进入后端处理。

示例：

```json
{
  "type": "turn_started",
  "with_tts": false,
  "message": "你好"
}
```

前端动作：

- 显示“处理中”
- 记录本轮开始时间

### 6.3 `agent_accepted`

表示 OpenClaw gateway 已经接受这一轮请求。

示例：

```json
{
  "type": "agent_accepted",
  "data": {
    "run_id": "xxx",
    "session_key": "agent:main:main",
    "status": "accepted"
  }
}
```

前端动作：

- 可选更新更细的状态文案，例如“已接单”
- 通常不必单独渲染成消息

### 6.4 `agent_delta`

这是最重要的实时文本事件。

示例：

```json
{
  "type": "agent_delta",
  "data": {
    "run_id": "xxx",
    "session_key": "agent:main:main",
    "delta": "你好",
    "reply": "你好，很高兴见到你。",
    "elapsed_ms": 1820
  }
}
```

前端渲染规则一定要这样做：

- 优先使用 `data.reply`
- 把 `data.reply` 当作“当前完整文本”覆盖渲染
- 不要简单把 `data.delta` append 到末尾

原因：

- 当前后端发出来的 `reply` 更适合作为累计全文
- 部分情况下可能会出现重复 `agent_delta`
- 如果前端按 append 模式处理，容易出现重复文本

正确做法：

- assistant bubble 的内容始终等于最近一次 `agent_delta.data.reply`

### 6.5 `agent_reply`

表示最终文本结果已经确定。

示例：

```json
{
  "type": "agent_reply",
  "data": {
    "kind": "ask",
    "status": "ok",
    "prompt": "你好",
    "reply": "你好，很高兴见到你。",
    "error": "",
    "session_id": "...",
    "bridge_session_key": "agent:main:main",
    "openclaw_elapsed_sec": 1.93,
    "bridge_timing": {
      "accept_ms": 121,
      "first_delta_ms": 1878,
      "final_ms": 1893,
      "history_ms": 42,
      "wait_ms": 1772,
      "total_ms": 1936
    },
    "tts": {
      "requested": false
    },
    "updated_at": 1776139381.857922
  }
}
```

前端动作：

- 用 `data.reply` 覆盖最终文本
- 如果不关心细节指标，`bridge_timing` 可以只打日志

### 6.6 `turn_done`

表示这一轮彻底结束。

示例：

```json
{
  "type": "turn_done",
  "data": {
    "turn": {},
    "status": {}
  }
}
```

前端动作：

- 把当前 assistant bubble 标记为完成
- 结束 loading
- 重新允许发送下一条消息
- 用 `status` 刷新顶部状态栏

注意：

- 聊天是否结束，以 `turn_done` 为准
- 不要把 `agent_reply` 当作整个链路彻底结束
- 如果 `with_tts=true`，`agent_reply` 之后还可能继续收到 TTS 事件

### 6.7 `error`

WebSocket 请求级错误。

示例：

```json
{
  "type": "error",
  "stage": "agent",
  "code": "AGENT_FAILED",
  "message": "..."
}
```

字段说明：

- `stage`: 常见值为 `request` / `agent` / `tts`
- `code`: 错误码
- `message`: 错误描述

前端动作：

- 结束当前 loading
- 恢复发送按钮
- 把当前轮标记为失败
- 给出 toast 或错误提示

## 7. TTS 相关事件

如果 `with_tts=true`，前端继续在同一条 WebSocket 上接收以下事件。

### 7.1 `tts_started`

表示后端已准备开始桥接 TTS。

### 7.2 `tts_unavailable`

表示这轮请求了 TTS，但当前后端判断 TTS 不可用。

前端动作：

- 文本照常展示
- UI 提示“语音暂不可用”
- 不要把整轮对话判成失败

### 7.3 `tts_session_ready`

表示远端 TTS session 建立成功。

这个事件主要用于调试，普通 UI 可以不展示。

### 7.4 `tts_llm_delta`

表示远端 TTS 内部的增量文本。

前端建议：

- 可以忽略
- 不建议拿它做逐字字幕

### 7.5 `tts_segment`

表示当前 TTS 正在处理的文本片段。

前端建议：

- 可选记录日志
- 普通用户界面通常不必展示

### 7.6 `tts_segment_done`

表示一个 TTS 分段已完成。

### 7.7 `audio_chunk`

这是真正需要播放的音频事件。

示例：

```json
{
  "type": "audio_chunk",
  "pcm16_base64": "...",
  "sample_rate": 44100
}
```

字段说明：

- `pcm16_base64`: base64 编码后的单声道 PCM16
- `sample_rate`: 当前块采样率

前端要做的事情：

1. base64 解码
2. 按 `Int16` 解析 PCM
3. 转成 Web Audio 可播放的浮点采样
4. 自己维护一个小缓冲队列顺序播放

注意：

- 这不是完整 mp3 / wav 文件
- 不能把 `pcm16_base64` 当作 URL 直接播

### 7.8 `tts_warning`

表示远端 TTS 在已有部分结果的情况下返回了非致命警告。

前端建议：

- 打日志即可
- 不一定要强制中断播放

### 7.9 `interrupted`

表示远端 TTS 被中断。

前端动作：

- 可以视为“语音部分提前结束”
- 文本轮次本身不一定失败

## 8. 前端 UI 规则

这部分是实现时最容易踩坑的地方。

### 8.1 一次只允许一轮进行中

当前后端是串行处理模型，同一时刻只允许一个 agent turn。

前端建议：

- 本轮未结束前禁用发送按钮
- 如果用户连续点击，直接拦住，不要并发发多个 `ask`

### 8.2 assistant 文本使用“覆盖更新”

正确方式：

- `assistantText = latestEvent.data.reply`

错误方式：

- `assistantText += latestEvent.data.delta`

### 8.3 页面重进时恢复上一轮

页面重进或 App 恢复时，建议补一次：

- `GET /api/v1/agent/last-turn`

用法：

- 如果上一轮是 `status=ok`，可恢复最后一条 assistant 结果
- 如果上一轮是 `status=error`，可恢复错误态

### 8.4 断线重连

如果 WebSocket 断开：

1. UI 进入“连接中”
2. 自动重连
3. 重连成功后等待 `ready`
4. 可选重新拉一次 `GET /api/v1/agent/status`
5. 可选重新拉一次 `GET /api/v1/agent/last-turn`

不要做的事：

- 不要在断线后直接重发上一条用户消息，除非业务层自己做了幂等保护

## 9. 是否还需要 warmup

结论：

- 不是必须
- 可以保留，但只是优化第一问冷启动的辅助手段

推荐理解：

- 主体验靠 `WS /api/v1/ws/agent-stream`
- `warmup` 只是“页面刚进入时，顺手把后端先热一下”

如果前端要简化流程，可以先不接 `warmup`。

## 10. REST 接口说明

### 10.1 `GET /api/v1/health`

用途：

- 判断控制器服务是否在线
- 顺便拿到 `session` 和 `agent` 概况

### 10.2 `GET /api/v1/agent/status`

用途：

- 页面初始化时读取当前状态
- 判断是否 `busy`
- 判断 TTS 是否可用

关键字段：

- `data.enabled`
- `data.busy`
- `data.session_id`
- `data.bridge_session_key`
- `data.last_error`
- `data.tts.enabled`
- `data.tts.available`
- `data.tts.last_error`
- `data.last_turn`

### 10.3 `GET /api/v1/agent/last-turn`

用途：

- 页面刷新后恢复最近一轮结果
- 调试用

### 10.4 `POST /api/v1/agent/reset-session`

用途：

- 用户主动点“新会话”
- 清掉当前上下文

### 10.5 `POST /api/v1/agent/warmup`

用途：

- 可选预热

示例：

```json
{
  "prompt": "请只回复“就绪”。"
}
```

### 10.6 `POST /api/v1/agent/ask`

不推荐作为聊天主入口。

适合：

- 后台调试
- 不需要流式文本的场景

不适合：

- 面向用户的聊天主界面

## 11. 一个最小可落地的前端状态机

推荐前端只维护一个简单状态机：

- `disconnected`
- `connecting`
- `ready`
- `running`
- `error`

状态切换建议：

- WebSocket 建立前: `connecting`
- 收到 `ready`: `ready`
- 发送 `ask` 后: `running`
- 收到 `turn_done`: `ready`
- 收到 `error`: `error`
- 用户确认后或重连成功后: `ready`

## 12. 伪代码示例

```ts
const ws = new WebSocket("ws://127.0.0.1:8010/api/v1/ws/agent-stream");

let currentAssistantText = "";
let running = false;

ws.onmessage = (event) => {
  const payload = JSON.parse(event.data);

  if (payload.type === "ready") {
    setConnectionState("ready");
    return;
  }

  if (payload.type === "turn_started") {
    running = true;
    currentAssistantText = "";
    showAssistantBubble("");
    showLoading(true);
    return;
  }

  if (payload.type === "agent_delta") {
    currentAssistantText = payload.data?.reply || payload.data?.delta || "";
    updateAssistantBubble(currentAssistantText);
    return;
  }

  if (payload.type === "agent_reply") {
    currentAssistantText = payload.data?.reply || currentAssistantText;
    updateAssistantBubble(currentAssistantText);
    return;
  }

  if (payload.type === "audio_chunk") {
    enqueuePcm16(payload.pcm16_base64, payload.sample_rate);
    return;
  }

  if (payload.type === "turn_done") {
    running = false;
    showLoading(false);
    setConnectionState("ready");
    return;
  }

  if (payload.type === "error") {
    running = false;
    showLoading(false);
    showError(payload.message || "Agent request failed");
    setConnectionState("error");
  }
};

function sendAsk(message: string, withTts = false) {
  if (running) return;

  ws.send(JSON.stringify({
    type: "ask",
    message,
    with_tts: withTts,
  }));
}
```

## 13. 给前端同事的最短版本

请按下面方式接：

- 初始化：
  - `GET /api/v1/health`
  - `GET /api/v1/agent/status`
  - 建立 `WS /api/v1/ws/agent-stream`
- 主聊天：
  - 全部走 WebSocket `ask`
- 文本渲染：
  - 用 `agent_delta.data.reply` 做覆盖更新
- 语音播放：
  - 收 `audio_chunk`，本地解码 PCM16 播放
- 新会话：
  - `POST /api/v1/agent/reset-session`
- 页面恢复：
  - `GET /api/v1/agent/last-turn`

一句话总结：

- 不要再把 `POST /api/v1/agent/ask` 当聊天主流程
- 聊天页应该使用一条长连接 WebSocket，边收 `agent_delta` 边渲染
