"""Robot tool dispatcher backed by momo_robot_service."""

from __future__ import annotations

import json
import os
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


MOMO_ROBOT_SERVICE_URL_DEFAULT = "http://127.0.0.1:8010"
ROBOT_TOOL_NAMES = frozenset(
    {
        "move_robot_arm",
        "get_robot_state",
        "stop_robot",
        "set_gripper",
        "rotate_joint",
        "run_robot_behavior",
        "run_robot_skill",
        "run_skill",
    }
)

OPENAI_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "move_robot_arm",
            "description": (
                "Move the robot TCP to a clear Cartesian target. Use only when the user gives an "
                "explicit target position; prefer small joint steps for vague movement requests."
            ),
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
            "description": "Get the current robot joints, TCP pose, and gripper state.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_robot",
            "description": "Stop robot motion immediately.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gripper",
            "description": "Set gripper open ratio. 0.0 is closed; 1.0 is open.",
            "parameters": {
                "type": "object",
                "properties": {
                    "open_ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0},
                    "wait": {"type": "boolean", "default": True},
                },
                "required": ["open_ratio"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rotate_joint",
            "description": "Rotate one named joint by delta degrees, or set it to an absolute angle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "joint_name": {"type": "string", "description": "Joint name, for example wrist_roll"},
                    "delta_deg": {"type": "number", "description": "Relative rotation in degrees"},
                    "target_deg": {"type": "number", "description": "Optional absolute joint angle in degrees"},
                    "duration": {"type": "number", "minimum": 0.2, "maximum": 20.0, "default": 1.0},
                    "wait": {"type": "boolean", "default": True},
                },
                "required": ["joint_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_robot_behavior",
            "description": (
                "Run a curated robot behavior by name, such as home, open_gripper, or close_gripper. "
                "Do not use this for Nanobot builtin skills."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Robot behavior name"},
                    "params": {"type": "object", "additionalProperties": True},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
]


def ensure_tools_schema_file(path: Optional[Path] = None) -> Path:
    target = Path(path) if path is not None else Path("/tmp/momo_robot_tools.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(OPENAI_TOOL_SCHEMAS, ensure_ascii=False, indent=2), encoding="utf-8")
    return target.resolve()


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _failure(code: str, message: str, *, request_id: str = "", tool: str = "") -> str:
    return _json(
        {
            "ok": False,
            "code": str(code or "TOOL_FAILED"),
            "error": str(message or "tool failed"),
            "request_id": str(request_id or ""),
            "tool": str(tool or ""),
        }
    )


class LocalToolDispatcher:
    """Dispatch Nanobot robot tools through the single robot service owner."""

    def __init__(
        self,
        mock_camera_path: Optional[Path] = None,
        tool_requester: Optional[Callable[[str, Dict[str, Any], str, float], Dict[str, Any]]] = None,
        tool_request_timeout_sec: float = 6.0,
    ):
        del mock_camera_path
        self._tool_requester = tool_requester
        self._tool_request_timeout_sec = max(2.0, float(tool_request_timeout_sec))
        self._service_url = (
            os.getenv("MOMO_ROBOT_SERVICE_URL", MOMO_ROBOT_SERVICE_URL_DEFAULT).strip()
            or MOMO_ROBOT_SERVICE_URL_DEFAULT
        ).rstrip("/")
        self._backend_mode = (
            os.getenv("MOMO_ROBOT_TOOL_BACKEND")
            or "service"
        ).strip().lower() or "service"

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
            return _failure(
                "INVALID_TOOL_RESPONSE",
                "tool requester returned invalid response",
                request_id=request_id,
                tool=tool_name,
            )
        result = response.get("result", {})
        payload = dict(result) if isinstance(result, dict) else {"value": result}
        payload.setdefault("ok", bool(response.get("ok", False)))
        payload.setdefault("request_id", request_id)
        payload.setdefault("tool", tool_name)
        return _json(payload)

    def _post_service_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._service_url}/api/v1/tools/dispatch",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self._tool_request_timeout_sec) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                parsed = {}
            error = parsed.get("error") if isinstance(parsed, dict) else {}
            if isinstance(error, dict):
                return {
                    "ok": False,
                    "code": str(error.get("code", "") or f"HTTP_{exc.code}"),
                    "error": str(error.get("message", "") or exc.reason),
                }
            return {"ok": False, "code": f"HTTP_{exc.code}", "error": str(exc.reason)}
        except urllib.error.URLError as exc:
            return {"ok": False, "code": "SERVICE_UNAVAILABLE", "error": str(exc.reason)}
        except TimeoutError:
            return {"ok": False, "code": "SERVICE_TIMEOUT", "error": "momo_robot_service timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "SERVICE_FAILED", "error": str(exc)}

        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return {"ok": False, "code": "INVALID_SERVICE_RESPONSE", "error": "service returned non-JSON"}
        return parsed if isinstance(parsed, dict) else {"ok": False, "code": "INVALID_SERVICE_RESPONSE", "error": "service returned invalid JSON"}

    def _dispatch_via_service(self, tool_name: str, args: Dict[str, Any]) -> str:
        request_id = str(uuid.uuid4())
        response = self._post_service_json(
            {
                "name": str(tool_name),
                "arguments": dict(args or {}),
                "request_id": request_id,
                "timeout_sec": float(self._tool_request_timeout_sec),
            }
        )
        if not bool(response.get("ok", False)):
            return _failure(
                str(response.get("code", "") or "SERVICE_FAILED"),
                str(response.get("error", "") or "momo_robot_service request failed"),
                request_id=request_id,
                tool=tool_name,
            )
        data = response.get("data", {})
        payload = dict(data) if isinstance(data, dict) else {"ok": True, "result": data}
        payload.setdefault("request_id", request_id)
        payload.setdefault("tool", tool_name)
        return _json(payload)

    def _dispatch_mock(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name == "get_robot_state":
            return _json(
                {
                    "ok": True,
                    "backend": "explicit-mock",
                    "connected": False,
                    "joints_rad": {},
                    "ee_xyz_m": [],
                    "ee_rpy_rad": [],
                    "gripper": {"available": False},
                }
            )
        return _json(
            {
                "ok": True,
                "backend": "explicit-mock",
                "tool": tool_name,
                "arguments": dict(args or {}),
                "message": "mock result; no robot command was sent",
            }
        )

    def dispatch(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        tool_name = str(name or "").strip()
        args = dict(arguments or {})
        if tool_name not in ROBOT_TOOL_NAMES:
            raise ValueError(f"Unknown robot tool: {tool_name}")

        if self._backend_mode in {"mock", "explicit-mock", "explicit_mock"}:
            return self._dispatch_mock(tool_name, args)
        if self._backend_mode in {"none", "off", "disabled"}:
            return _failure("TOOLS_DISABLED", "robot tools are disabled", tool=tool_name)
        if self._tool_requester is not None:
            return self._dispatch_via_tool_requester(tool_name, args)
        if self._backend_mode not in {"service", "api", "momo_robot_service", "auto"}:
            return _failure(
                "INVALID_BACKEND",
                f"unsupported robot tool backend: {self._backend_mode}",
                tool=tool_name,
            )
        return self._dispatch_via_service(tool_name, args)


__all__ = [
    "LocalToolDispatcher",
    "OPENAI_TOOL_SCHEMAS",
    "ensure_tools_schema_file",
]
