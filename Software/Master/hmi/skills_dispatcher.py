"""Local tool dispatcher for robot action mock execution."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


OPENAI_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "move_robot_arm",
            "description": "Move robot TCP in Cartesian space in meters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Target x in meters"},
                    "y": {"type": "number", "description": "Target y in meters"},
                    "z": {"type": "number", "description": "Target z in meters"},
                    "frame": {"type": "string", "enum": ["base", "tool"], "default": "base"},
                    "duration": {"type": "number", "minimum": 0.2, "maximum": 20.0, "default": 2.0},
                    "wait": {"type": "boolean", "default": True},
                },
                "required": ["x", "y", "z"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_robot_state",
            "description": "Get current robot joints and TCP pose.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_frame",
            "description": "Capture one frame from virtual camera.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["eye_in_hand", "scene"], "default": "eye_in_hand"},
                    "width": {"type": "integer", "minimum": 160, "maximum": 1920, "default": 960},
                    "height": {"type": "integer", "minimum": 120, "maximum": 1080, "default": 720},
                    "format": {"type": "string", "enum": ["jpg", "png"], "default": "jpg"},
                    "return_mode": {"type": "string", "enum": ["path", "base64"], "default": "path"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_robot",
            "description": "Emergency stop robot motion immediately.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Run a higher-level robot behavior skill by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name, e.g. dance_short or grasp_apple_mock",
                    },
                    "params": {
                        "type": "object",
                        "description": "Optional parameters for selected skill",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
]


def ensure_tools_schema_file(path: Optional[Path] = None) -> Path:
    target = Path(path) if path is not None else Path("/tmp/mocearm_openclaw_tools.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(OPENAI_TOOL_SCHEMAS, ensure_ascii=False, indent=2), encoding="utf-8")
    return target.resolve()


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


class LocalToolDispatcher:
    """Mock local dispatcher for robot tools."""

    def __init__(
        self,
        mock_camera_path: Optional[Path] = None,
        tool_requester: Optional[Callable[[str, Dict[str, Any], str, float], Dict[str, Any]]] = None,
        tool_request_timeout_sec: float = 6.0,
    ):
        self._mock_camera_path = (
            Path(mock_camera_path) if mock_camera_path is not None else Path("/tmp/mocearm_mock_camera_frame.jpg")
        )
        self._tool_requester = tool_requester
        self._tool_request_timeout_sec = max(2.0, float(tool_request_timeout_sec))
        self._backend_mode = str(os.getenv("MOCEARM_TOOL_BACKEND", "auto")).strip().lower() or "auto"
        self._tool_module = None
        self._tool_module_error = ""
        self._try_load_mocearm_tools()

    def _try_load_mocearm_tools(self):
        if self._backend_mode in ("mock", "none", "off"):
            return
        repo_root = Path(__file__).resolve().parents[3]
        module_path = repo_root / "skills" / "mocearm-openclaw-control" / "scripts" / "mocearm_tools.py"
        if not module_path.exists():
            self._tool_module_error = f"mocearm_tools.py not found: {module_path}"
            return
        try:
            module_name = f"mocearm_tools_runtime_{os.getpid()}"
            spec = importlib.util.spec_from_file_location(module_name, str(module_path))
            if spec is None or spec.loader is None:
                raise RuntimeError("failed to create module spec")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            if not hasattr(mod, "call_tool"):
                raise RuntimeError("call_tool not found in mocearm_tools.py")
            self._tool_module = mod
        except Exception as exc:
            self._tool_module = None
            self._tool_module_error = str(exc)

    def _dispatch_real_if_available(self, name: str, args: Dict[str, Any]) -> Optional[str]:
        if self._tool_requester is not None:
            return None
        if self._tool_module is None:
            if self._backend_mode in ("real", "mocearm", "mocearm_tools"):
                raise RuntimeError(f"real backend unavailable: {self._tool_module_error or 'unknown'}")
            return None
        try:
            result = self._tool_module.call_tool(name, args)
        except Exception as exc:
            if self._backend_mode in ("real", "mocearm", "mocearm_tools"):
                raise
            # In auto mode, fallback to mock when real backend fails.
            self._tool_module_error = str(exc)
            return None
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)

    def _dispatch_via_tool_requester(self, tool_name: str, args: Dict[str, Any]) -> str:
        if self._tool_requester is None:
            raise RuntimeError("tool requester is not configured")
        request_id = str(uuid.uuid4())
        response = self._tool_requester(
            str(tool_name),
            dict(args or {}),
            request_id,
            float(self._tool_request_timeout_sec),
        )
        if not isinstance(response, dict):
            return json.dumps(
                {
                    "ok": False,
                    "error": "tool requester returned invalid response",
                    "request_id": request_id,
                },
                ensure_ascii=False,
            )
        ok = bool(response.get("ok", False))
        result = response.get("result", {})
        if isinstance(result, dict):
            payload = dict(result)
        else:
            payload = {"value": result}
        payload.setdefault("request_id", request_id)
        payload.setdefault("ok", ok)
        return json.dumps(payload, ensure_ascii=False)

    def move_robot_arm(
        self,
        x: float,
        y: float,
        z: float,
        frame: str = "base",
        duration: float = 2.0,
        wait: bool = True,
    ) -> str:
        x = _to_float(x, 0.0)
        y = _to_float(y, 0.0)
        z = _to_float(z, 0.0)
        duration = max(0.2, min(20.0, _to_float(duration, 2.0)))
        frame = "tool" if str(frame).strip().lower() == "tool" else "base"
        wait = bool(wait)
        msg = (
            f"move_robot_arm 执行成功: target=({x:.3f}, {y:.3f}, {z:.3f}) m, "
            f"frame={frame}, duration={duration:.2f}s, wait={wait}"
        )
        print(msg)
        return msg

    def get_robot_state(self) -> str:
        state = {
            "ok": True,
            "mode": "simulation-mock",
            "joints_rad": {
                "shoulder": 0.120,
                "shoulder_lift": -0.340,
                "elbow": 0.560,
                "wrist": 0.100,
                "wrist_roll": -0.020,
                "gripper": 0.300,
            },
            "tcp_pose": {
                "position_m": {"x": 0.302, "y": 0.006, "z": 0.181},
                "quaternion_xyzw": {"x": 0.0, "y": 0.707, "z": 0.0, "w": 0.707},
            },
        }
        return json.dumps(state, ensure_ascii=False)

    def get_camera_frame(
        self,
        source: str = "eye_in_hand",
        width: int = 960,
        height: int = 720,
        format: str = "jpg",
        return_mode: str = "path",
    ) -> str:
        source = "scene" if str(source).strip().lower() == "scene" else "eye_in_hand"
        width = max(160, min(1920, _to_int(width, 960)))
        height = max(120, min(1080, _to_int(height, 720)))
        fmt = "png" if str(format).strip().lower() == "png" else "jpg"
        mode = "base64" if str(return_mode).strip().lower() == "base64" else "path"

        path = self._mock_camera_path
        if fmt == "png":
            path = path.with_suffix(".png")
        else:
            path = path.with_suffix(".jpg")

        if not path.exists():
            # 1x1 white JPEG.
            jpeg_1x1 = (
                b"/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQEBUQEA8PEA8QDw8PEA8PDw8QDxAQFREWFhUR"
                b"FRUYHSggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKDg0OGxAQGi0fHyUtLS0tLS0tLS0tLS0t"
                b"LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLf/AABEIAAEAAgMBIgACEQEDEQH/xAAXAAEAAwAA"
                b"AAAAAAAAAAAAAAABAgME/8QAFhEBAQEAAAAAAAAAAAAAAAAAABEB/9oADAMBAAIQAxAAAAGtQ0//xAAb"
                b"EAACAgMBAAAAAAAAAAAAAAABAgADESEEEv/aAAgBAQABPwC2M2hN2r0k7//EABYRAQEBAAAAAAAAAAAA"
                b"AAAAAAABEf/aAAgBAgEBPwBv/8QAFhEBAQEAAAAAAAAAAAAAAAAAABEB/9oACAEDAQE/AYf/xAAaEAAC"
                b"AgMAAAAAAAAAAAAAAAABEQAhMUFh/9oACAEBAAY/ApjQxFf/xAAaEAEAAgMBAAAAAAAAAAAAAAABABEh"
                b"MUFh/9oACAEBAAE/IfaJ5S3r6Q2Q9qf/2gAMAwEAAgADAAAAEM//xAAXEQADAQAAAAAAAAAAAAAAAAAA"
                b"AREx/9oACAEDAQE/EAtf/8QAFxEAAwEAAAAAAAAAAAAAAAAAAAERMf/aAAgBAgEBPxBWf//EABsQAQEA"
                b"AgMBAAAAAAAAAAAAAAERACExQVFh/9oACAEBAAE/EEQ4uUE1RjQ7h7kF8j//2Q=="
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(jpeg_1x1))

        if mode == "base64":
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            out = {
                "ok": True,
                "source": source,
                "width": width,
                "height": height,
                "format": fmt,
                "return_mode": mode,
                "base64": encoded,
            }
            return json.dumps(out, ensure_ascii=False)

        out = {
            "ok": True,
            "source": source,
            "width": width,
            "height": height,
            "format": fmt,
            "return_mode": mode,
            "path": str(path.resolve()),
        }
        return json.dumps(out, ensure_ascii=False)

    def stop_robot(self) -> str:
        msg = "stop_robot 执行成功: emergency stop triggered"
        print(msg)
        return msg

    def dispatch(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        args = dict(arguments or {})
        func = str(name or "").strip()

        if self._tool_requester is not None and func in (
            "move_robot_arm",
            "get_robot_state",
            "get_camera_frame",
            "stop_robot",
            "run_skill",
        ):
            return self._dispatch_via_tool_requester(func, args)

        real = self._dispatch_real_if_available(func, args)
        if real is not None:
            return real

        if func == "move_robot_arm":
            return self.move_robot_arm(
                x=args.get("x", 0.0),
                y=args.get("y", 0.0),
                z=args.get("z", 0.0),
                frame=args.get("frame", "base"),
                duration=args.get("duration", 2.0),
                wait=args.get("wait", True),
            )
        if func == "get_robot_state":
            return self.get_robot_state()
        if func == "get_camera_frame":
            return self.get_camera_frame(
                source=args.get("source", "eye_in_hand"),
                width=args.get("width", 960),
                height=args.get("height", 720),
                format=args.get("format", "jpg"),
                return_mode=args.get("return_mode", "path"),
            )
        if func == "stop_robot":
            return self.stop_robot()
        if func == "run_skill":
            name = str(args.get("name", "")).strip()
            params = args.get("params", {})
            if not isinstance(params, dict):
                params = {}
            if not name:
                raise ValueError("run_skill requires non-empty name")
            # Mock fallback behavior when no main-thread requester is provided.
            if name in ("dance_short", "dance", "wave"):
                return json.dumps(
                    {"ok": True, "skill": name, "message": "dance skill executed (mock)"},
                    ensure_ascii=False,
                )
            if name in ("grasp_apple", "grasp_apple_mock", "pick_apple"):
                return json.dumps(
                    {
                        "ok": True,
                        "skill": name,
                        "message": "grasp apple skill executed (mock)",
                        "target": {"label": "red apple"},
                    },
                    ensure_ascii=False,
                )
            raise ValueError(f"unsupported skill: {name}")
        raise ValueError(f"Unknown tool: {func}")


__all__ = [
    "LocalToolDispatcher",
    "OPENAI_TOOL_SCHEMAS",
    "ensure_tools_schema_file",
]
