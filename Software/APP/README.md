# MomoAgent Mobile MVP

一个最小 Android MVP，用来验证这条链路：

- 手机本地语音识别转文本
- `quick_control_api` `WS /api/v1/ws/agent-stream` 流式对话
- 后端流式 TTS `audio_chunk` 在手机本地直接播放
- `face_loc` 独立视频预览接口 `GET /frame.jpg`

## 当前能力

- 输入控制器 IP
- 连接 `agent-stream`
- 手动输入文本并发送
- 调起安卓系统语音识别
- 接收 `agent_delta` 实时更新回复文本
- 接收 `audio_chunk` 并播放 PCM16 单声道流式音频
- 独立轮询摄像头 JPEG 预览

## 默认端口

- Agent: `8010`
- Preview: `8000`

如果你的 `face_loc` 不在 `8000`，改 App 里的预览端口即可。

## 依赖的后端接口

聊天：

- `ws://<host>:8010/api/v1/ws/agent-stream`

视频预览：

- `http://<host>:8000/frame.jpg?max_width=960&quality=70`

## 后端准备

1. 启动 `quick_control_api`
2. 启动 `face_loc`
3. 确保手机与电脑在同一局域网
4. 在 App 中填入电脑 IP

## 构建

等本地 Android SDK 与 JDK 安装完成后，在本目录执行：

```bash
./gradlew assembleDebug
```

APK 默认输出到：

```text
app/build/outputs/apk/debug/app-debug.apk
```
