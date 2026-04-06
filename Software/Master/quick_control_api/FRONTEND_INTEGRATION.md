# Quick Control API Frontend Integration

这份文档只写前端真正需要对接的部分。

当前边界：

- 前端 / App 主要负责用户交互、语音输入输出、展示状态
- `follow` 和 `idle_scan` 属于 backend / agent 行为层，不建议前端直接调
- 前端如果只是和 agent 对话，只需要接 `agent` 相关接口，加一个只读 `robot/state`

## Base URL

- `http://<controller-ip>:8010/api/v1`

## 前端推荐使用的接口

### 1. 健康检查

- `GET /api/v1/health`

用途：
- 判断控制器服务是否在线
- 读取 session 概况
- 读取 agent 概况

### 2. 会话状态

- `GET /api/v1/session/status`
- `POST /api/v1/session/connect`
- `POST /api/v1/session/disconnect`

说明：
- 如果前端需要显示“机械臂已连接/未连接”，读这个即可
- `POST /api/v1/session/connect` 请求体固定可用：

```json
{
  "prefer_real": true,
  "allow_sim_fallback": true
}
```

### 3. 机器人只读状态

- `GET /api/v1/robot/state`
- `WS /api/v1/ws/state`

推荐前端展示这些字段：
- `session.connected`
- `control_mode`
- `control_error`
- `joint_state`
- `tcp_pose`
- `gripper`

说明：
- `follow` / `idle_scan` 的状态也会出现在 `robot/state` 里
- 但前端通常只需要展示，不需要主动调用它们的 start/stop

### 4. 文本 Agent

- `GET /api/v1/agent/status`
- `GET /api/v1/agent/last-turn`
- `POST /api/v1/agent/warmup`
- `POST /api/v1/agent/reset-session`
- `POST /api/v1/agent/ask`
- `WS /api/v1/ws/agent`

最小调用顺序：

1. 页面进入后先调用 `POST /api/v1/agent/warmup`
2. 用户输入文本后调用 `POST /api/v1/agent/ask`
3. 用 `GET /api/v1/agent/status` 或 `WS /api/v1/ws/agent` 展示 agent 当前状态

`POST /api/v1/agent/ask` 请求体：

```json
{
  "message": "你好，请帮我开始跟随画面中的人"
}
```

## 前端不建议直接调用的接口

下面这些接口保留给 backend / agent 行为层：

- `POST /api/v1/follow/start`
- `POST /api/v1/follow/stop`
- `GET /api/v1/follow/status`
- `POST /api/v1/idle-scan/start`
- `POST /api/v1/idle-scan/stop`
- `GET /api/v1/idle-scan/status`

原因：

- `follow` 现在是后端直接轮询 `face_loc /latest`
- `idle_scan` 是后端内部待机巡航行为
- 前端直接管这些模式会把职责边界搅乱

## 手动控制

如果前端仍然需要提供“人工接管”页面，可以使用：

- `POST /api/v1/motion/joint-step`
- `POST /api/v1/motion/cartesian-jog`
- `POST /api/v1/motion/home`
- `POST /api/v1/motion/stop`

但如果前端当前阶段只是做 agent 交互，这部分可以先不接。

## 错误格式

所有失败响应统一格式：

```json
{
  "ok": false,
  "error": {
    "code": "NOT_CONNECTED",
    "message": "Robot is not connected"
  }
}
```

常见错误：

- `NOT_CONNECTED`
- `CONNECT_FAILED`
- `CARTESIAN_FAILED`

## 给前端同事的最短结论

前端现阶段只需要接这几类：

- `health`
- `session/status`
- `robot/state`
- `agent/*`
- `ws/state`
- `ws/agent`

`follow` 和 `idle_scan` 不要从前端直接调，把它们当作 agent/back-end 内部能力即可。
