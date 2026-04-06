# Momo Agent

轻量版展会代理入口，目标是替代 GUI 里的重型语音窗口链路，只保留：

- 语音输入
- STT 转写
- OpenClaw 调用
- TTS 播放

它不会触碰真实机械臂 SDK 的运动逻辑，只负责做人机交互和 OpenClaw 转发。

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

仅预热当前 OpenClaw session：

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
- `/warmup`：预热当前 OpenClaw session
- `/reset`：清空本地缓存 session
- `/quit`：退出

## 主要环境变量

- `OPENCLAW_SKILL_NAME`
- `OPENCLAW_BIN`
- `OPENCLAW_TIMEOUT_SEC`
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
