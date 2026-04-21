# Momo Agent

轻量版展会代理入口，目标是替代 GUI 里的重型语音窗口链路，只保留：

- 语音输入
- STT 转写
- Agent backend 调用（`OpenClaw` 或 `Nanobot`）
- TTS 播放

它不会触碰真实机械臂 SDK 的运动逻辑，只负责人机交互和 agent backend 转发。

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
python Software/Master/momo_agent/main.py ask 帮我把机械臂移动到演示位
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
- `/session`：查看当前配置里的 session
- `/warmup`：预热当前 agent session
- `/reset`：清空本地缓存 session
- `/quit`：退出

## 主要环境变量

- `MOMO_AGENT_BACKEND`
- `OPENCLAW_SKILL_NAME`
- `OPENCLAW_BIN`
- `OPENCLAW_TIMEOUT_SEC`
- `MOMO_AGENT_NANOBOT_API_BASE`
- `MOMO_AGENT_NANOBOT_MODEL`
- `MOMO_AGENT_NANOBOT_SOURCE_DIR`
- `MOMO_AGENT_NANOBOT_CONFIG_PATH`
- `MOMO_AGENT_NANOBOT_WORKSPACE`
- `MOMO_AGENT_NANOBOT_TIMEOUT_SEC`
- `MOMO_AGENT_NANOBOT_TOOL_MODE`
- `MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS`
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

默认会沿用仓库根目录和 `Software/Master` 下的 `.env` / `env` 文件。

## Nanobot PoC

桥接版 `Nanobot` backend 现在支持 3 种工具模式：

- `bridge_only`
  只注册机器人桥接工具：`move_robot_arm`、`get_robot_state`、`stop_robot`、`set_gripper`、`rotate_joint`、`run_robot_behavior`
- `hybrid`
  保留 nanobot 默认文件/搜索工具，再叠加机器人桥接工具
- `all`
  在 `hybrid` 基础上再打开 `exec`、`web`、`my`，并取消 workspace 限制，适合直接读取任意 `SKILL.md` 或执行 skill 里的 `curl`

默认建议就是：

- `MOMO_AGENT_NANOBOT_TOOL_MODE=all`
- `MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS=0`
- `MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS=24`

这样会上线 nanobot 原生 builtin skills，同时保留我们自己的机器人桥接工具。

推荐最小配置：

```bash
export MOMO_AGENT_BACKEND=nanobot
export MOMO_AGENT_NANOBOT_API_BASE=http://172.18.29.16:1234/v1
export MOMO_AGENT_NANOBOT_MODEL=qwen/qwen3.5-35b-a3b
export MOMO_AGENT_NANOBOT_TOOL_MODE=all
export MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS=0
export MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS=24
```

## GitHub 源码接入

如果你想直接基于上游开源仓库改 `nanobot` 本体，而不是只依赖 PyPI 包，推荐走仓库内置脚本：

```bash
bash scripts/bootstrap_nanobot.sh --install-native-skill-deps

export MOMO_AGENT_BACKEND=nanobot
export MOMO_AGENT_NANOBOT_SOURCE_DIR=$PWD/external/nanobot
export MOMO_AGENT_NANOBOT_API_BASE=http://172.18.29.16:1234/v1
export MOMO_AGENT_NANOBOT_MODEL=qwen/qwen3.5-35b-a3b
export MOMO_AGENT_NANOBOT_TOOL_MODE=all
export MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS=0
export MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS=24

bash scripts/run_momo_nanobot.sh warmup
bash scripts/run_momo_nanobot.sh ask 帮我看看当前机械臂状态
```

说明：

- `scripts/bootstrap_nanobot.sh` 会自动 clone / pull `HKUDS/nanobot` 到 `external/nanobot`
- 它会创建独立环境 `.venv-nanobot`
- bridge 运行时会优先加载 `MOMO_AGENT_NANOBOT_SOURCE_DIR` 指向的本地源码
- `scripts/run_momo_nanobot.sh` 现在默认会把 `MOMO_AGENT_NANOBOT_TOOL_MODE` 设成 `all`
- `scripts/run_momo_nanobot.sh` 现在默认启用 nanobot builtin skills
- `bash scripts/bootstrap_nanobot.sh --install-native-skill-deps` 会补装 `tmux` 和 `summarize` 这些 builtin skills 常用依赖

`nanobot-ai` / 上游 `nanobot` 都要求 Python 3.11+。如果当前环境低于 3.11，请继续使用 `OpenClaw` backend。
