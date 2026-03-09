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
1. Validate parameters.
2. Call SDK.
3. Return structured result.

## Tool Mapping

- `move_robot_arm` -> `robot.move_tcp(...)` then `robot.get_state()`
- `move_robot_delta` -> relative TCP offset move then `robot.get_state()`
- `get_robot_state` -> `robot.get_state()`
- `set_gripper` -> `robot.set_gripper(...)`
- `open_gripper` -> `set_gripper(open_ratio=1.0)`
- `close_gripper` -> `set_gripper(open_ratio=0.0)`
- `stop_robot` -> `robot.stop()`

Do not expose camera tools until SDK camera APIs are published.

## Motion Arguments

`move_robot_arm` canonical args:
- `x`, `y`, `z`
- optional `tool_pitch`, `tool_roll`
- optional `frame` (`base` or `tool`)
- optional `duration`, `wait`, `timeout`

If `tool_pitch/tool_roll` are omitted, SDK keeps the current task orientation.

`move_robot_delta` canonical args:
- `dx`, `dy`, `dz`
- optional `frame` (`base` or `tool`)
- optional `duration`, `wait`, `timeout`

## Relative Command Rule

For relative user intent such as `抬高一点` or `靠近一点`:
1. Prefer `move_robot_delta` directly.
2. If `move_robot_delta` is unavailable, call `get_robot_state`.
3. Compute absolute target in meters and call `move_robot_arm`.

## Frame Semantics

Use `base` as the default frame for natural-language motion unless the user clearly asks for motion relative to the tool/end-effector direction.

Rules:
- `base` frame means world/base coordinates.
- `tool` frame means end-effector local coordinates.
- Never use `tool` frame for `抬高/降低/升高/下降/向上/向下`.
- Only use `tool` frame for local end-effector motion such as `靠近一点/远离一点/向前一点/向后一点`.
- If the natural language is ambiguous, choose `base`.

## Natural Language Mapping

Default small-step offsets:
- `抬高一点` / `高一点` / `升高一点` / `向上一点` -> `{"dx": 0.0, "dy": 0.0, "dz": 0.01, "frame": "base"}`
- `降低一点` / `低一点` / `下降一点` / `向下一点` -> `{"dx": 0.0, "dy": 0.0, "dz": -0.01, "frame": "base"}`
- `靠近一点` / `向前一点` -> `{"dx": 0.01, "dy": 0.0, "dz": 0.0, "frame": "tool"}`
- `远离一点` / `向后一点` -> `{"dx": -0.01, "dy": 0.0, "dz": 0.0, "frame": "tool"}`

Command construction rules:
- Always prefer canonical arguments `dx`, `dy`, `dz`.
- Do not invent wrapper shapes such as `delta`, `xyz`, or `rpy` for `move_robot_delta`.
- For `move_robot_delta`, always include an explicit `frame`.
- If the user asks to `raise` the arm, the expected effect is `base z` increasing.
- If the user asks to `lower` the arm, the expected effect is `base z` decreasing.

For gripper natural language:
- `夹爪闭合/抓住` -> `close_gripper` or `set_gripper(open_ratio=0.0)`
- `夹爪打开/松开` -> `open_gripper` or `set_gripper(open_ratio=1.0)`

## CLI Shortcuts

- `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py call --name <tool> --args '<json>'`
- `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py get_robot_state`
- `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py move_robot_arm --dz 0.01 --frame base --wait true`
- `python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py set_gripper --close --wait true`

## Resources

- `scripts/soarmmoce_tools.py`: tool dispatcher and SDK adapter
- `references/tool_schemas.json`: OpenAI tools schema
