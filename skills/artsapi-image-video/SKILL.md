---
name: artsapi-image-video
description: 调用 ArtsAPI 的图生图与图生视频接口。用户要用 https://api.artsapi.com/api 做图生图、图生视频、查询视频任务状态、或查看当前可用 image/video 模型时使用这个 skill。
metadata:
  openclaw:
    emoji: "🎨"
    requires:
      bins: ["python3"]
---

# artsapi-image-video

## 何时使用

- 用户要调用 ArtsAPI 做图生图
- 用户要调用 ArtsAPI 做图生视频
- 用户要查询 ArtsAPI 视频任务状态
- 用户要查看 ArtsAPI 当前支持的 image/video 模型

## 密钥位置

- 默认从下面这个文件读取 API Key：
  `/Users/moce/.openclaw/skills/artsapi-image-video/config/artsapi.env`
- 在这个文件里填写：

```bash
ARTSAPI_API_KEY=在这里填写你的真实密钥
ARTSAPI_BASE_URL=https://api.artsapi.com/api
```

- 也可以临时用 `--api-key` 或环境变量 `ARTSAPI_API_KEY`

## 已确认的接口

- Base URL: `https://api.artsapi.com/api`
- 图像生成: `POST /v1/images/generations`
- 视频生成: `POST /v1/video/generations`
- 视频状态查询: `GET /v1/video/generations/{task_id}`

## 推荐命令

查看当前可用图片模型：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py models --type image
```

查看当前可用视频模型：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py models --type video
```

图生图：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py image \
  --prompt "把这张图改成电影海报风格" \
  --image-url "https://example.com/source.jpg"
```

图生图，也可以直接传本地图片路径：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py image \
  --prompt "把这张图改成电影海报风格" \
  --image-url "/Users/moce/Documents/example.jpg"
```

图生图并自动保存生成结果到本地：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py image \
  --prompt "把这张图改成电影海报风格" \
  --image-url "/Users/moce/Documents/example.jpg" \
  --save-local
```

多图融合图生图：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py image \
  --prompt "融合这些图片的风格，做成统一品牌海报" \
  --image-url "https://example.com/1.jpg" \
  --image-url "https://example.com/2.jpg"
```

图生视频：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py video \
  --prompt "从这张图开始，镜头缓慢推进，风吹动头发" \
  --image-url "https://example.com/start.jpg" \
  --duration 5 \
  --resolution 720p \
  --ratio 16:9
```

查询视频任务状态：

```bash
python3 /Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py status task_xxx
```

## 工作规则

1. 先用 `models` 看当前模型名，再决定 `--model`，尽量不要硬写过时模型名。
2. 图生图参考图用 `--image-url`；传 1 次是单图，传多次是多图融合。
3. 图生视频参考图也用 `--image-url`；传 1 张通常表示首帧，传 2 张表示首尾帧。
4. `--image-url` 现在支持三种输入：公网 `http/https` URL、`data:` URL、本地图片路径。
5. 传本地图片路径时，脚本会自动转成 `data:` URL 再提交。这是兼容模式，不是 ArtsAPI 页面文档里明确写出的官方本地上传接口；如果某个模型拒绝这种写法，就改用公网 URL。
6. 本地路径只接受图片文件，当前内联大小上限是 20MB。
7. `video` 命令默认会轮询直到成功、失败或超时；如果只想提交任务，使用 `--no-poll`。
8. 使用 `--save-local` 时，脚本会优先把生成结果保存到本地目录，再把本地文件路径一并放进返回 JSON 的 `_local_artifacts.saved_files`。
9. 图生图在 `--save-local` 且未显式指定 `--response-format` 时，会优先请求 `b64_json`，避免只拿到可能失效的临时 URL。
10. 失败时原样返回接口错误 JSON；如果下载云端结果失败，也会把原因写进 `_local_artifacts.save_errors`。

## 默认模型

- 图生图默认：`doubao-seedream-5-0-260128`
- 图生视频默认：`doubao-seedance-1-5-pro-251215`

如需切换模型，直接传 `--model 模型名`。
