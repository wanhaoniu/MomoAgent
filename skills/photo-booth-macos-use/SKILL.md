---
name: photo-booth-macos-use
description: Open Photo Booth on macOS, switch back to the live preview if needed, trigger one still photo through macos-use, and return the saved photo path from Photo Booth Library. Use when the user wants a coordinate-free one-shot capture flow, including webcam sources like DJI Pocket 3.
---

# photo-booth-macos-use

## 何时使用

- 用户要一键打开 `Photo Booth`
- 用户要自动切到实时预览并拍一张照片
- 用户当前摄像头来源已经在 `Photo Booth` 里选好
- 用户不想再依赖漂掉的绝对坐标

## 快速使用

直接拍一张：

```bash
bash /Users/moce/.openclaw/skills/photo-booth-macos-use/scripts/photo_booth_take_photo_macos_use.sh
```

给用户 5 秒准备时间再拍：

```bash
bash /Users/moce/.openclaw/skills/photo-booth-macos-use/scripts/photo_booth_take_photo_macos_use.sh --before-shot-delay 5
```

拍完在 Finder 里显示照片：

```bash
bash /Users/moce/.openclaw/skills/photo-booth-macos-use/scripts/photo_booth_take_photo_macos_use.sh --reveal
```

## 工作方式

1. 由 OpenClaw 启动一个 `Terminal` 子进程来代跑真实拍照脚本
2. 真实拍照脚本再用 `macos-use` 打开或激活 `Photo Booth`
3. 如果当前在回看界面，就点 `查看视频预览`
4. 找到 `拍照` 按钮并点击
5. 轮询 `~/Pictures/Photo Booth Library/Recents.plist`
6. 返回最新生成的照片路径

## 注意事项

- 依赖本机已可用的 `macos-use`：
  `/Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py`
- OpenClaw 下之所以通过 `Terminal` 代跑，是为了继承 `Terminal` 已有的辅助功能权限，绕开 `openclaw-gateway` 的权限限制
- 依赖 `Photo Booth` 已经选好正确的视频源，比如 `DJI Pocket 3`
- `Photo Booth` 默认可能会有倒计时，所以实际出图时间会晚几秒
- 如果用户切到了录像或四格模式，脚本会优先尝试回到普通拍照模式
