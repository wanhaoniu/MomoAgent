#!/usr/bin/env python3
"""OpenClaw tool dispatcher for SoarmMoce SDK.

Contract:
- Input: name + arguments(dict)
- Success: {"ok": true, "result": {...}, "error": null}
- Failure: {"ok": false, "result": {}, "error": {"type": "...", "message": "..."}}
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from dataclasses import is_dataclass
from typing import Any, Callable, Dict, List, Optional


class ValidationError(ValueError):
    """Raised when tool arguments are invalid."""


def _cli_bool(value: str) -> bool:
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value!r}")


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "move_robot_arm",
            "description": "Move robot TCP in Cartesian space using SDK move_tcp, then return latest robot state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Target x in meters."},
                    "y": {"type": "number", "description": "Target y in meters."},
                    "z": {"type": "number", "description": "Target z in meters."},
                    "tool_pitch": {"type": "number", "description": "Optional tool pitch in radians."},
                    "tool_roll": {"type": "number", "description": "Optional tool roll in radians."},
                    "frame": {"type": "string", "enum": ["base", "tool"], "default": "base"},
                    "duration": {"type": "number", "default": 2.0},
                    "wait": {"type": "boolean", "default": True},
                    "timeout": {"type": "number", "description": "Optional wait timeout in seconds."},
                },
                "required": ["x", "y", "z"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_robot_delta",
            "description": "Move robot TCP by relative offsets (dx,dy,dz) and return latest state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dx": {"type": "number", "description": "Offset x in meters."},
                    "dy": {"type": "number", "description": "Offset y in meters."},
                    "dz": {"type": "number", "description": "Offset z in meters."},
                    "frame": {"type": "string", "enum": ["base", "tool"], "default": "base"},
                    "duration": {"type": "number", "default": 2.0},
                    "wait": {"type": "boolean", "default": True},
                    "timeout": {"type": "number", "description": "Optional wait timeout in seconds."},
                },
                "required": ["dx", "dy", "dz"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_robot_state",
            "description": "Get current robot state from SDK.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gripper",
            "description": "Set gripper open ratio with SDK.",
            "parameters": {
                "type": "object",
                "properties": {
                    "open_ratio": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "0.0 closed, 1.0 open.",
                    },
                    "wait": {"type": "boolean", "default": True},
                    "timeout": {"type": "number", "description": "Optional wait timeout in seconds."},
                },
                "required": ["open_ratio"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_gripper",
            "description": "Open gripper fully.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wait": {"type": "boolean", "default": True},
                    "timeout": {"type": "number", "description": "Optional wait timeout in seconds."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_gripper",
            "description": "Close gripper fully.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wait": {"type": "boolean", "default": True},
                    "timeout": {"type": "number", "description": "Optional wait timeout in seconds."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_robot",
            "description": "Stop robot motion immediately via SDK.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


_ROBOT_LOCK = threading.Lock()
_ROBOT = None
_DEFAULT_MOCK_SHARED_STATE_FILE = "/tmp/soarmmoce_mock_shared_state.json"


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return _to_jsonable(value.tolist())
    if is_dataclass(value):
        return _to_jsonable(value.__dict__)
    if hasattr(value, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "result": result, "error": None}


def _fail(exc: Exception) -> Dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }


def _require_dict(arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValidationError("arguments must be a dict")
    return arguments


def _ensure_allowed_keys(arguments: Dict[str, Any], allowed_keys: set[str]) -> None:
    extras = sorted(set(arguments.keys()) - allowed_keys)
    if extras:
        raise ValidationError(f"Unexpected arguments: {extras}")


def _require_number(arguments: Dict[str, Any], key: str) -> float:
    if key not in arguments:
        raise ValidationError(f"Missing required argument: {key}")
    value = arguments[key]
    if not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number")
    return float(value)


def _optional_number(
    arguments: Dict[str, Any],
    key: str,
    default: Optional[float] = None,
    allow_none: bool = False,
) -> Optional[float]:
    if key not in arguments:
        return default
    value = arguments[key]
    if value is None:
        if allow_none:
            return None
        raise ValidationError(f"{key} cannot be null")
    if not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number")
    return float(value)


def _optional_bool(arguments: Dict[str, Any], key: str, default: Optional[bool] = None) -> Optional[bool]:
    if key not in arguments:
        return default
    value = arguments[key]
    if not isinstance(value, bool):
        raise ValidationError(f"{key} must be a boolean")
    return value


def _optional_str(arguments: Dict[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    if key not in arguments:
        return default
    value = arguments[key]
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string")
    return str(value).strip()



def _parse_xyz_array(raw: Any, field: str) -> List[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValidationError(f"{field} must be an array of exactly 3 numbers")
    out: List[float] = []
    for idx, value in enumerate(raw):
        if not isinstance(value, (int, float)):
            raise ValidationError(f"{field}[{idx}] must be a number")
        out.append(float(value))
    return out


def _parse_delta_offsets(arguments: Dict[str, Any]) -> tuple[float, float, float]:
    seen_any = False
    dx = dy = dz = None

    # Canonical keys
    for key in ("dx", "dy", "dz"):
        if key in arguments:
            seen_any = True
    dx = _optional_number(arguments, "dx", None)
    dy = _optional_number(arguments, "dy", None)
    dz = _optional_number(arguments, "dz", None)

    # Alias: x/y/z
    if "x" in arguments:
        seen_any = True
        if dx is None:
            dx = _optional_number(arguments, "x", None)
    if "y" in arguments:
        seen_any = True
        if dy is None:
            dy = _optional_number(arguments, "y", None)
    if "z" in arguments:
        seen_any = True
        if dz is None:
            dz = _optional_number(arguments, "z", None)

    # Alias: xyz array
    if "xyz" in arguments:
        seen_any = True
        xyz = _parse_xyz_array(arguments["xyz"], "xyz")
        if dx is None:
            dx = xyz[0]
        if dy is None:
            dy = xyz[1]
        if dz is None:
            dz = xyz[2]

    # Alias: delta object / array
    if "delta" in arguments:
        seen_any = True
        raw_delta = arguments["delta"]
        if isinstance(raw_delta, dict):
            if "dx" in raw_delta and dx is None:
                if not isinstance(raw_delta["dx"], (int, float)):
                    raise ValidationError("delta.dx must be a number")
                dx = float(raw_delta["dx"])
            if "dy" in raw_delta and dy is None:
                if not isinstance(raw_delta["dy"], (int, float)):
                    raise ValidationError("delta.dy must be a number")
                dy = float(raw_delta["dy"])
            if "dz" in raw_delta and dz is None:
                if not isinstance(raw_delta["dz"], (int, float)):
                    raise ValidationError("delta.dz must be a number")
                dz = float(raw_delta["dz"])
            if "x" in raw_delta and dx is None:
                if not isinstance(raw_delta["x"], (int, float)):
                    raise ValidationError("delta.x must be a number")
                dx = float(raw_delta["x"])
            if "y" in raw_delta and dy is None:
                if not isinstance(raw_delta["y"], (int, float)):
                    raise ValidationError("delta.y must be a number")
                dy = float(raw_delta["y"])
            if "z" in raw_delta and dz is None:
                if not isinstance(raw_delta["z"], (int, float)):
                    raise ValidationError("delta.z must be a number")
                dz = float(raw_delta["z"])
            if "xyz" in raw_delta:
                dxyz = _parse_xyz_array(raw_delta["xyz"], "delta.xyz")
                if dx is None:
                    dx = dxyz[0]
                if dy is None:
                    dy = dxyz[1]
                if dz is None:
                    dz = dxyz[2]
        elif isinstance(raw_delta, (list, tuple)):
            dxyz = _parse_xyz_array(raw_delta, "delta")
            if dx is None:
                dx = dxyz[0]
            if dy is None:
                dy = dxyz[1]
            if dz is None:
                dz = dxyz[2]
        else:
            raise ValidationError("delta must be an object or [x, y, z] array")

    if not seen_any:
        raise ValidationError("move_robot_delta requires at least one offset argument")

    dx = 0.0 if dx is None else float(dx)
    dy = 0.0 if dy is None else float(dy)
    dz = 0.0 if dz is None else float(dz)
    if abs(dx) < 1e-12 and abs(dy) < 1e-12 and abs(dz) < 1e-12:
        raise ValidationError("move_robot_delta requires a non-zero offset")
    return dx, dy, dz


def _get_robot():
    global _ROBOT
    with _ROBOT_LOCK:
        # Keep OpenClaw tool executions on the same mock state source as GUI by default.
        if not str(os.getenv("SOARMMOCE_MOCK_SHARED_STATE_FILE", "")).strip():
            os.environ["SOARMMOCE_MOCK_SHARED_STATE_FILE"] = _DEFAULT_MOCK_SHARED_STATE_FILE
        if _ROBOT is None:
            from soarmmoce_sdk import Robot  # Lazy import so schema mode works without SDK install.

            _ROBOT = Robot()
        if not getattr(_ROBOT, "connected", False):
            _ROBOT.connect()
        return _ROBOT


def _tool_move_robot_arm(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(
        arguments,
        {"x", "y", "z", "dx", "dy", "dz", "tool_pitch", "tool_roll", "frame", "duration", "wait", "timeout"},
    )
    dx = _optional_number(arguments, "dx", 0.0)
    dy = _optional_number(arguments, "dy", 0.0)
    dz = _optional_number(arguments, "dz", 0.0)
    use_delta = any(k in arguments for k in ("dx", "dy", "dz"))

    tool_pitch = _optional_number(arguments, "tool_pitch", None)
    tool_roll = _optional_number(arguments, "tool_roll", None)
    frame = str(arguments.get("frame", "base") or "base").strip().lower()
    if frame not in {"base", "tool"}:
        raise ValidationError("frame must be 'base' or 'tool'")
    duration = _optional_number(arguments, "duration", 2.0)
    wait = _optional_bool(arguments, "wait", True)
    timeout = _optional_number(arguments, "timeout", allow_none=True)

    robot = _get_robot()
    state_before = robot.get_state()

    if use_delta:
        if frame == "tool":
            x = float(dx)
            y = float(dy)
            z = float(dz)
        else:
            pos = state_before.tcp_pose.xyz
            x = float(pos[0] + float(dx))
            y = float(pos[1] + float(dy))
            z = float(pos[2] + float(dz))
        mode = "relative"
    else:
        x_raw = _optional_number(arguments, "x", None)
        y_raw = _optional_number(arguments, "y", None)
        z_raw = _optional_number(arguments, "z", None)
        if x_raw is None and y_raw is None and z_raw is None:
            raise ValidationError("At least one of x/y/z or dx/dy/dz is required")
        if frame == "tool":
            # tool frame semantics are offsets; fill missing with zero offset.
            x = 0.0 if x_raw is None else float(x_raw)
            y = 0.0 if y_raw is None else float(y_raw)
            z = 0.0 if z_raw is None else float(z_raw)
        else:
            # base frame semantics are absolute pose; fill missing from current state.
            pos = state_before.tcp_pose.xyz
            x = float(pos[0]) if x_raw is None else float(x_raw)
            y = float(pos[1]) if y_raw is None else float(y_raw)
            z = float(pos[2]) if z_raw is None else float(z_raw)
        mode = "absolute"

    q = robot.move_tcp(
        x=x,
        y=y,
        z=z,
        tool_pitch=tool_pitch,
        tool_roll=tool_roll,
        frame=frame,
        duration=duration,
        wait=wait,
        timeout=timeout,
    )
    state = robot.get_state()
    result = {
        "command": {
            "mode": mode,
            "x": x,
            "y": y,
            "z": z,
            "dx": dx if use_delta else None,
            "dy": dy if use_delta else None,
            "dz": dz if use_delta else None,
            "tool_pitch": tool_pitch,
            "tool_roll": tool_roll,
            "frame": frame,
            "duration": duration,
            "wait": wait,
            "timeout": timeout,
        },
        "move_return": _to_jsonable(q),
        "state": _to_jsonable(state),
    }
    return _ok(result)


def _tool_get_robot_state(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(arguments, set())
    robot = _get_robot()
    state = robot.get_state()
    return _ok({"state": _to_jsonable(state)})


def _tool_set_gripper(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(
        arguments,
        {
            "open_ratio",
            "ratio",
            "target",
            "value",
            "action",
            "state",
            "open",
            "close",
            "wait",
            "timeout",
        },
    )

    open_ratio: Optional[float] = _optional_number(arguments, "open_ratio", None)
    if open_ratio is None:
        open_ratio = _optional_number(arguments, "ratio", None)
    if open_ratio is None:
        open_ratio = _optional_number(arguments, "target", None)
    if open_ratio is None:
        open_ratio = _optional_number(arguments, "value", None)

    if open_ratio is None:
        action = (_optional_str(arguments, "action", "") or _optional_str(arguments, "state", "") or "").lower()
        if action in {"open", "opened", "release"}:
            open_ratio = 1.0
        elif action in {"close", "closed", "grasp", "clamp"}:
            open_ratio = 0.0

    if open_ratio is None and bool(arguments.get("open", False)):
        open_ratio = 1.0
    if open_ratio is None and bool(arguments.get("close", False)):
        open_ratio = 0.0

    if open_ratio is None:
        raise ValidationError("Missing required argument: open_ratio")

    if not (0.0 <= open_ratio <= 1.0):
        raise ValidationError("open_ratio must be in [0.0, 1.0]")
    wait = _optional_bool(arguments, "wait", True)
    timeout = _optional_number(arguments, "timeout", allow_none=True)

    robot = _get_robot()
    robot.set_gripper(open_ratio=open_ratio, wait=wait, timeout=timeout)
    state = robot.get_state()
    result = {
        "command": {"open_ratio": open_ratio, "wait": wait, "timeout": timeout},
        "state": _to_jsonable(state),
    }
    return _ok(result)


def _tool_stop_robot(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(arguments, set())
    robot = _get_robot()
    robot.stop()
    return _ok({"stopped": True})


def _tool_move_robot_delta(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(
        arguments,
        {
            "dx",
            "dy",
            "dz",
            "x",
            "y",
            "z",
            "xyz",
            "delta",
            "rx",
            "ry",
            "rz",
            "frame",
            "duration",
            "wait",
            "timeout",
        },
    )
    dx, dy, dz = _parse_delta_offsets(arguments)
    mapped = {
        "dx": dx,
        "dy": dy,
        "dz": dz,
        "frame": arguments.get("frame", "base"),
        "duration": arguments.get("duration", 2.0),
        "wait": arguments.get("wait", True),
        "timeout": arguments.get("timeout", None),
    }
    return _tool_move_robot_arm(mapped)


def _tool_open_gripper(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(arguments, {"wait", "timeout"})
    mapped = {
        "open_ratio": 1.0,
        "wait": arguments.get("wait", True),
        "timeout": arguments.get("timeout", None),
    }
    return _tool_set_gripper(mapped)


def _tool_close_gripper(arguments: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_allowed_keys(arguments, {"wait", "timeout"})
    mapped = {
        "open_ratio": 0.0,
        "wait": arguments.get("wait", True),
        "timeout": arguments.get("timeout", None),
    }
    return _tool_set_gripper(mapped)


TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "move_robot_arm": _tool_move_robot_arm,
    "move_robot_delta": _tool_move_robot_delta,
    "get_robot_state": _tool_get_robot_state,
    "set_gripper": _tool_set_gripper,
    "open_gripper": _tool_open_gripper,
    "close_gripper": _tool_close_gripper,
    "stop_robot": _tool_stop_robot,
}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name not in TOOL_HANDLERS:
            raise ValidationError(f"Unknown tool name: {name}")
        args = _require_dict(arguments)
        return TOOL_HANDLERS[name](args)
    except Exception as exc:
        return _fail(exc)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="SoarmMoce OpenClaw tool dispatcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("schema", help="Print OpenAI tool schemas")

    call = sub.add_parser("call", help="Call tool by name + arguments JSON")
    call.add_argument("--name", required=True, help="Tool name")
    call.add_argument("--args", default="{}", help="Tool arguments as JSON object string")

    cmd_state = sub.add_parser("get_robot_state", help="Shortcut: call get_robot_state")
    cmd_state.set_defaults(_tool_name="get_robot_state")

    cmd_stop = sub.add_parser("stop_robot", help="Shortcut: call stop_robot")
    cmd_stop.set_defaults(_tool_name="stop_robot")

    cmd_move = sub.add_parser("move_robot_arm", help="Shortcut: call move_robot_arm")
    cmd_move.add_argument("--x", type=float, default=None)
    cmd_move.add_argument("--y", type=float, default=None)
    cmd_move.add_argument("--z", type=float, default=None)
    cmd_move.add_argument("--dx", type=float, default=None)
    cmd_move.add_argument("--dy", type=float, default=None)
    cmd_move.add_argument("--dz", type=float, default=None)
    cmd_move.add_argument("--tool-pitch", type=float, default=None)
    cmd_move.add_argument("--tool-roll", type=float, default=None)
    cmd_move.add_argument("--frame", default="base", choices=["base", "tool"])
    cmd_move.add_argument("--duration", type=float, default=2.0)
    cmd_move.add_argument("--wait", type=_cli_bool, default=True)
    cmd_move.add_argument("--timeout", type=float, default=None)
    cmd_move.set_defaults(_tool_name="move_robot_arm")

    cmd_gripper = sub.add_parser("set_gripper", help="Shortcut: call set_gripper")
    cmd_gripper.add_argument("--open-ratio", type=float, default=None)
    cmd_gripper.add_argument("--open", action="store_true")
    cmd_gripper.add_argument("--close", action="store_true")
    cmd_gripper.add_argument("--wait", type=_cli_bool, default=True)
    cmd_gripper.add_argument("--timeout", type=float, default=None)
    cmd_gripper.set_defaults(_tool_name="set_gripper")

    args = parser.parse_args()

    if args.cmd == "schema":
        print(json.dumps(TOOL_SCHEMAS, ensure_ascii=False, indent=2))
        return

    if args.cmd == "call":
        try:
            payload = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(json.dumps(_fail(ValidationError(f"Invalid JSON in --args: {exc}")), ensure_ascii=False))
            return
        result = call_tool(args.name, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd in {"get_robot_state", "stop_robot"}:
        result = call_tool(getattr(args, "_tool_name"), {})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "move_robot_arm":
        payload: Dict[str, Any] = {
            "frame": args.frame,
            "duration": args.duration,
            "wait": args.wait,
            "timeout": args.timeout,
        }
        for key in ("x", "y", "z", "dx", "dy", "dz", "tool_pitch", "tool_roll"):
            value = getattr(args, key)
            if value is not None:
                payload[key] = value
        result = call_tool("move_robot_arm", payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "set_gripper":
        payload = {"wait": args.wait, "timeout": args.timeout}
        if args.open_ratio is not None:
            payload["open_ratio"] = args.open_ratio
        if bool(args.open):
            payload["open"] = True
        if bool(args.close):
            payload["close"] = True
        result = call_tool("set_gripper", payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    _cli()
