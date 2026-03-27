---
name: magic-mirror-demo
description: 魔镜 demo 技能。用户说“魔镜拍照”“帮我拍一张”“魔镜请拍照”时，用 soarmmoce-real-con 的本地摄像头抓拍脚本把照片保存到这个 skill 的工作空间；用户说“谁最美”“选最美的人”“把最美的人变成皇后”“生成皇后视频”时，从工作空间照片里选出最佳候选，先把本地照片上传成 ArtsAPI 可访问的公网图片 URL，再调用 ArtsAPI 图生视频生成皇后变身视频并优先保存到本地。
metadata:
  openclaw:
    emoji: "🪞"
    requires:
      bins: ["python3"]
---

# magic-mirror-demo

## 何时使用

- 用户说：`魔镜拍照`
- 用户说：`魔镜请拍照`
- 用户说：`帮我拍一张`
- 用户说：`谁是最美的人`
- 用户说：`选最美的人`
- 用户说：`把最美的人变成皇后`
- 用户说：`生成皇后视频`

## 核心规则

1. 这个 demo 的拍照不要走 `photo-booth-camera` 或 DJI Pocket/Photo Booth 那套链路。
2. 拍照必须走 `soarmmoce-real-con/scripts/soarmmoce_camera_snap.py` 对应的本地摄像头抓拍能力。
3. 拍到的照片统一存到本 skill 的工作空间：
   `/Users/moce/.openclaw/skills/magic-mirror-demo/workspace/runtime/photos`
4. “最美的人”当前不是审美模型，而是一个演示用的人像候选启发式：
   优先看人脸占比、是否居中、人脸检测置信度、清晰度、亮度是否合适。
5. 用户说“皇后视频”“把最美的人变成皇后”时，先从工作空间里选最佳候选，再把本地照片上传成公网 URL，然后调用 ArtsAPI 图生视频。
6. 图生视频结果优先保存到本地：
   `/Users/moce/.openclaw/skills/magic-mirror-demo/workspace/runtime/generated`
7. 如果 `queen-video` 的 `--image-path` 本身已经是公网 `http/https` URL，就直接复用，不再重复上传。
8. 拍照完成后，系统会自动用 `open` 命令在屏幕上弹出预览。
9. “选最美的人” (`pick-best`) 找到最佳照片后，也会自动 `open` 预览。## 推荐命令

### 魔镜拍照

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py capture
```

如果 USB 摄像头在第一个索引，推荐直接这样：

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py capture \
  --camera-device 0
```

如果需要指定分辨率：

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py capture \
  --camera-device 0 \
  --camera-width 1280 \
  --camera-height 720
```

### 从已保存照片里选最佳候选

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py pick-best
```

### 把最佳候选变成皇后视频

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py queen-video
```

如果你想自定义视频提示词：

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py queen-video \
  --prompt "让照片中的人物缓慢变身为高贵皇后，出现金色王冠和华丽礼服，镜头缓慢推进，童话魔镜氛围"
```

如果你想直接指定某一张照片而不是自动选最佳：

```bash
python3 /Users/moce/.openclaw/skills/magic-mirror-demo/scripts/magic_mirror_demo.py queen-video \
  --image-path "/absolute/path/to/photo.jpg"
```

## 工作流

1. 用户说“魔镜拍照”时，运行 `capture`。
2. 新照片会写入 `workspace/runtime/photos`。
3. 用户说“谁最美”“选最美的人”时，运行 `pick-best`。
4. 用户说“把最美的人变成皇后”“生成皇后视频”时，运行 `queen-video`。
5. `queen-video` 会自动：
   - 从 `workspace/runtime/photos` 里挑最佳候选
   - 把那张本地照片上传成公网 URL
   - 把那张图片 URL 送到 ArtsAPI 图生视频
   - 把结果优先保存到 `workspace/runtime/generated`

## 依赖

- 摄像头抓拍：
  `/Users/moce/.openclaw/skills/soarmmoce-real-con/scripts/soarmmoce_camera_snap.py`
- 图生视频：
  `/Users/moce/.openclaw/skills/artsapi-image-video/scripts/artsapi_cli.py`
- ArtsAPI 密钥：
  `/Users/moce/.openclaw/skills/artsapi-image-video/config/artsapi.env`
- 本地图片公网桥接：
  当前默认使用 `catbox.moe` 匿名上传

## 已知限制

- “最美的人”当前是演示启发式，不是真正的美学评分模型。
- 如果当前 USB 摄像头不是索引 `0`，需要改 `--camera-device`。
- 皇后视频的真实生成仍然依赖你已经配置好的 ArtsAPI key。
- 当前默认的公网图片桥接是匿名公共 URL，适合 demo，不适合私密内容。
