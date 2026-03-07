---
name: soarmmoce-control
description: Use this skill when implementing or running OpenClaw function-calling for SoarmMoce robot control through soarmmoce_sdk, including move_robot_arm/get_robot_state/set_gripper/stop_robot with a fixed name+arguments dispatcher and standardized {ok,result,error} tool responses.
---

# SoarmMoce Control

Use this skill to expose and run SDK-backed tool calls for the arm.

## Runtime Contract

Input:
- `name: str`
- `arguments: dict`

Output:
- Success: `{"ok": true, "result": {...}, "error": null}`
- Failure: `{"ok": false, "result": {}, "error": {"type": "...", "message": "..."}}`

Only do three things:
1) Validate parameters.
2) Call SDK.
3) Return structured result.

## Tool Mapping

- `move_robot_arm` -> `robot.move_tcp(...)` then `robot.get_state()`
- `move_robot_delta` -> relative TCP offset move then `robot.get_state()`
- `get_robot_state` -> `robot.get_state()`
- `set_gripper` -> `robot.set_gripper(...)`
- `open_gripper` -> `set_gripper(open_ratio=1.0)`
- `close_gripper` -> `set_gripper(open_ratio=0.0)`
- `stop_robot` -> `robot.stop()`

Do not expose camera tools until SDK camera APIs are published.

## Relative Command Rule

For relative user intent such as "抬高一点" or "靠近一点":
1) Prefer `move_robot_delta` directly.
2) If `move_robot_delta` is unavailable, call `get_robot_state`.
3) Compute absolute target in meters and call `move_robot_arm`.

`move_robot_delta` canonical args:
- `dx`, `dy`, `dz` (meters), optional `frame` (`base` or `tool`)
- Example: `{"dx": 0.0, "dy": 0.0, "dz": 0.01, "frame": "base"}`

Default small-step offsets:
- `高一点`: `z + 0.01`
- `低一点`: `z - 0.01`
- `靠近一点`: `frame="tool"` and `x + 0.01`

For gripper natural language:
- `夹爪闭合/抓住` -> `close_gripper` (or `set_gripper(open_ratio=0.0)`)
- `夹爪打开/松开` -> `open_gripper` (or `set_gripper(open_ratio=1.0)`)

## CLI Shortcuts

The dispatcher supports both generic and shortcut commands:
- Generic: `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py call --name <tool> --args '<json>'`
- Shortcuts:
  - `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py get_robot_state`
  - `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py move_robot_arm --dz 0.01 --frame base --wait true`
  - `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py set_gripper --close --wait true`

## Resources

- `scripts/soarmmoce_tools.py`: tool dispatcher and SDK adapter
- `references/tool_schemas.json`: OpenAI tools schema (current 7 tools)
