# Momo Agent

轻量版展会代理入口，只负责人机交互链路：

- 语音输入
- STT 转写
- Nanobot agent 调用
- TTS 播放

机械臂硬件会话只属于 `momo_robot_service`。Momo Agent 不直接触碰 SDK，不启动第二个机器人进程；当 Nanobot 需要看状态或执行动作时，只能通过已注册的机器人工具转到 `momo_robot_service`。

## 启动

交互 shell：

```bash
python Software/Master/momo_agent/main.py
```

或者显式写：

```bash
python Software/Master/momo_agent/main.py shell
```

单次文本调用：

```bash
python Software/Master/momo_agent/main.py ask 帮我看看当前机械臂状态
```

单次语音调用：

```bash
python Software/Master/momo_agent/main.py voice
```

长驻展会模式，保持同一个进程和同一个 warm session：

```bash
python Software/Master/momo_agent/main.py listen --warmup
```

只播报 TTS：

```bash
python Software/Master/momo_agent/main.py say 欢迎来到展会现场
```

仅预热当前 agent session：

```bash
python Software/Master/momo_agent/main.py warmup
```

重置缓存的 session：

```bash
python Software/Master/momo_agent/main.py reset-session
```

## Shell 命令

- `/voice`：录音一轮，按 Enter 结束
- `/say <text>`：只做语音播报
- `/session`：查看当前 Nanobot session
- `/warmup`：预热当前 agent session
- `/reset`：清空本地缓存 session
- `/quit`：退出

## 主要环境变量

默认会沿用仓库根目录和 `Software/Master` 下的 `.env` / `env` 文件。

Nanobot:

- `MOMO_AGENT_NANOBOT_ENABLED`
- `MOMO_AGENT_NANOBOT_API_BASE`
- `MOMO_AGENT_NANOBOT_MODEL`
- `MOMO_AGENT_NANOBOT_API_KEY`
- `MOMO_AGENT_NANOBOT_SOURCE_DIR`
- `MOMO_AGENT_NANOBOT_CONFIG_PATH`
- `MOMO_AGENT_NANOBOT_WORKSPACE`
- `MOMO_AGENT_NANOBOT_TIMEOUT_SEC`
- `MOMO_AGENT_NANOBOT_TOOL_MODE`
- `MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS`
- `MOMO_AGENT_NANOBOT_SESSION_KEY`
- `MOMO_AGENT_NANOBOT_SESSION_KEY_PREFIX`
- `MOMO_AGENT_NANOBOT_FORCE_NEW_SESSION`

模型/API fallback:

- `AUTOGRASP_VLM_API_BASE`
- `AUTOGRASP_VLM_MODEL`
- `AUTOGRASP_VLM_API_KEY`
- `AUTOGRASP_VLM_REASONING_EFFORT`

STT/TTS:

- `SOARMMOCE_STT_URL`
- `SOARMMOCE_STT_MODEL`
- `SOARMMOCE_STT_API_KEY`
- `SOARMMOCE_TTS_ENABLED`
- `SOARMMOCE_TTS_PROVIDER`
- `SOARMMOCE_TTS_URL`
- `SOARMMOCE_TTS_MODEL`
- `SOARMMOCE_TTS_VOICE`
- `SOARMMOCE_TTS_API_KEY`
- `SOARMMOCE_TTS_PLAYBACK_BACKEND`
- `MOMO_AGENT_MAX_RECORD_SEC`

## Nanobot 配置

默认工具模式是 `bridge_only`：

```bash
export MOMO_AGENT_NANOBOT_API_BASE=http://127.0.0.1:1234/v1
export MOMO_AGENT_NANOBOT_MODEL=qwen/qwen3.5-35b-a3b
export MOMO_AGENT_NANOBOT_TOOL_MODE=bridge_only
export MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS=24
```

`bridge_only` 只注册机器人桥接工具：

- `get_robot_state`
- `rotate_joint`
- `move_robot_arm`
- `set_gripper`
- `stop_robot`
- `run_robot_behavior`

这些工具会转发到 `momo_robot_service` 的统一后端状态。不要让 Nanobot 使用 shell、curl、直接 SDK import 或独立进程来控制机械臂。

## 本地源码接入

如果你想直接基于上游开源仓库改 `nanobot` 本体，而不是只依赖 PyPI 包，可以把源码放到 `external/nanobot`，然后配置：

```bash
export MOMO_AGENT_NANOBOT_SOURCE_DIR=$PWD/external/nanobot
export MOMO_AGENT_NANOBOT_API_BASE=http://127.0.0.1:1234/v1
export MOMO_AGENT_NANOBOT_MODEL=qwen/qwen3.5-35b-a3b
export MOMO_AGENT_NANOBOT_TOOL_MODE=bridge_only
```

运行时会优先加载 `MOMO_AGENT_NANOBOT_SOURCE_DIR` 指向的本地源码。`nanobot-ai` / 上游 `nanobot` 都要求 Python 3.11+。
