---
name: macos-use-desktop-control
description: Use the local macos-use MCP server to open Mac apps, traverse accessibility trees, click elements by visible text, type text, press keys, and scroll. Use when the user wants desktop UI control on macOS with accessibility-aware element search instead of fixed screen coordinates.
---

# macos-use-desktop-control

## 何时使用

- 用户要在 macOS 上做桌面控制，而不是只靠固定坐标点击
- 用户要按元素文字查找并点击按钮、链接、文本框
- 用户要打开应用、发按键、输入文本、刷新当前 UI 树
- 需要比纯坐标脚本更稳的桌面自动化入口

## 快速使用

列出当前可用工具：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py tools
```

打开或激活一个应用，并返回它的 PID 和 UI 树摘要：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py open --app com.apple.TextEdit
```

刷新某个应用当前的 UI 树：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py refresh --pid 12345
```

按文字查找并点击元素：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py click-text --pid 12345 --text 打开
```

如果要限定角色，比如只找按钮：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py click-text --pid 12345 --text 打开 --role AXButton
```

输入文本：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py type --pid 12345 --text "hello test"
```

发送按键：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py key --pid 12345 --key RightArrow
```

滚动：

```bash
python3 /Users/moce/.openclaw/skills/macos-use-desktop-control/scripts/macos_use_control.py scroll --pid 12345 --x 600 --y 400 --delta-y 8
```

## 注意事项

- 这个 skill 依赖本机已构建好的 `macos-use`：
  `/Users/moce/.openclaw/mcp/macos-use`
- 更稳的应用标识一般是 `bundle id` 或 `.app` 路径，而不是纯名称。
- `click-text` 是模糊匹配，命中多个元素时会点第一个可见匹配项。
- `macos-use` 仍然依赖 macOS 辅助功能权限；如果当前宿主没有权限，动作会失败。
- 需要查看更完整的元素树时，优先使用返回摘要里的 `file` 路径继续搜索。
