---
name: sidecar-window-control
description: Move the current frontmost macOS window, or a named app window, onto the iPad Sidecar or extended display using the local MouseClickHelper service. Use when the user wants to switch the Mac presentation window onto the iPad screen for a demo.
---

# sidecar-window-control

## 何时使用

- 用户要把当前 Mac 窗口切到 iPad 扩展屏/Sidecar
- 用户要把某个指定应用窗口放到 iPad 屏上演示
- 用户要先确认扩展屏边界，再移动窗口

## 快速使用

启动 helper 服务：

```bash
bash /Users/moce/.openclaw/skills/sidecar-window-control/scripts/sidecar_window_control.sh start-server
```

查看 Sidecar/扩展屏边界：

```bash
bash /Users/moce/.openclaw/skills/sidecar-window-control/scripts/sidecar_window_control.sh frame
```

把当前前台窗口移到 Sidecar：

```bash
bash /Users/moce/.openclaw/skills/sidecar-window-control/scripts/sidecar_window_control.sh move-front-window
```

把某个应用窗口移到 Sidecar：

```bash
bash /Users/moce/.openclaw/skills/sidecar-window-control/scripts/sidecar_window_control.sh move-front-window --process Books
```

## 注意事项

- 这个 skill 只控制 Mac 窗口到扩展屏，不控制原生 iPad app。
- 首次使用时，需要给 `MouseClickHelper.app` 开辅助功能权限。
- 如果 Sidecar 断开或显示器排列变了，先重新跑一次 `frame`。
