---
name: screen-pointer-tools
description: 读取当前鼠标坐标、查看显示器边界、通过本地 helper 服务执行点击/按键/应用切换，并把鼠标向指定边缘推进或连续扫动；适用于扩展屏、Sidecar 和固定坐标演示前的录点与指针定位。
---

# screen-pointer-tools

## 何时使用

- 用户要获取当前鼠标坐标
- 用户要查看当前多显示器的坐标范围
- 用户要把鼠标推向某个屏幕边缘
- 用户要为固定点击脚本重新录点

## 快速使用

先确保本地 helper 服务已经起来：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh start-server
```

如果是第一次使用，先弹出辅助功能授权：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh trust
```

读取当前鼠标坐标：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh point --json
```

查看当前显示器边界：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh displays
```

查看 helper 服务和权限状态：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh health
```

点击指定坐标：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh click --x 514.22 --y 37.21
```

向当前前台应用发送按键：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh key --key RightArrow
```

切到指定应用：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh activate --identifier com.apple.iBooksX
```

查看 Sidecar/扩展屏可见区域：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh sidecar-frame
```

把当前前台窗口移到 Sidecar/扩展屏：

```bash
bash /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/mouse_click_helper.sh sidecar-move-front-window
```

向左边缘推进鼠标：

```bash
swift /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/move_mouse_to_ipad.swift push --edge left
```

沿左边缘连续扫动：

```bash
swift /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/move_mouse_to_ipad.swift sweep --edge left --cycles 0
```

预演而不真实移动：

```bash
swift /Users/moce/.openclaw/skills/screen-pointer-tools/scripts/move_mouse_to_ipad.swift sweep --edge left --cycles 1 --dry-run
```

## 注意事项

- `point --json`、`displays` 和真实鼠标事件现在使用同一套坐标系。
- OpenClaw 下优先通过 `mouse_click_helper.sh` 访问本地 `MouseClickHelper.app` 服务，而不是直接在 gateway 进程里发鼠标事件。
- 首次使用时，把 `MouseClickHelper.app` 加到：
  `系统设置 > 隐私与安全性 > 辅助功能`
- `push` 适合短促推进，`sweep` 适合连续找回光标。
- `sidecar-move-front-window` 只控制 Mac 窗口到扩展屏，不控制原生 iPad app。
