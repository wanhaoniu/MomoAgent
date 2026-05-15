"""PhyAgentOS HAL driver for the MomoAgent arm.

The driver is intentionally thin: it adapts PhyAgentOS action names to the
runtime bridge. The bridge imports `soarmmoce_sdk` directly and owns the real
robot command for that invocation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from hal.base_driver import BaseDriver
except Exception:  # pragma: no cover - lets local tests run without PhyAgentOS installed
    class BaseDriver:  # type: ignore[no-redef]
        pass


_PACKAGE_ROOT = Path(__file__).resolve().parent
_PROFILES_DIR = _PACKAGE_ROOT / "profiles"
_PLUGIN_ROOT = _PACKAGE_ROOT.parent
_DEFAULT_RUNTIME_ROOT = _PLUGIN_ROOT / "runtime"


_ACTION_ALIASES: dict[str, str] = {
    "preflight": "preflight",
    "momo_preflight": "preflight",
    "state": "state",
    "get_state": "state",
    "robot_state": "state",
    "momo_state": "state",
    "joint_delta": "joint_delta",
    "robot_joint_delta": "joint_delta",
    "momo_joint_delta": "joint_delta",
    "joint_target": "joint_target",
    "robot_joint_target": "joint_target",
    "momo_joint_target": "joint_target",
    "joints_target": "joints_target",
    "robot_joints_target": "joints_target",
    "momo_joints_target": "joints_target",
    "cartesian_delta": "cartesian_delta",
    "robot_cartesian_delta": "cartesian_delta",
    "momo_cartesian_delta": "cartesian_delta",
    "pose": "pose",
    "move_pose": "pose",
    "move_to": "pose",
    "momo_pose": "pose",
    "set_gripper": "gripper",
    "gripper": "gripper",
    "robot_gripper": "gripper",
    "momo_gripper": "gripper",
    "open_gripper": "open_gripper",
    "close_gripper": "close_gripper",
    "home": "home",
    "robot_home": "home",
    "momo_home": "home",
    "stop": "stop",
    "robot_stop": "stop",
    "momo_stop": "stop",
    "torque": "torque",
    "enable_torque": "enable_torque",
    "disable_torque": "disable_torque",
}


class MomoAgentDriver(BaseDriver):
    """HAL driver that delegates real MomoAgent control to `runtime/momo_bridge.py`."""

    def __init__(self, gui: bool = False, **kwargs: Any) -> None:
        self._gui = bool(gui)
        self._scene: dict[str, dict] = {}
        self._last_runtime: dict[str, Any] = {}
        self._runtime_root = self._resolve_runtime_root(kwargs.get("momo_runtime_root"))
        self._bridge_script = self._runtime_root / "momo_bridge.py"
        self._python_bin = str(
            kwargs.get("momo_python")
            or os.environ.get("MOMOAGENT_PYTHON")
            or sys.executable
        )
        self._default_timeout_s = float(kwargs.get("timeout_s") or os.environ.get("MOMOAGENT_TIMEOUT_S") or 30.0)

    def get_profile_path(self) -> Path:
        return _PROFILES_DIR / "momoagent.md"

    def load_scene(self, scene: dict[str, dict]) -> None:
        self._scene = dict(scene)

    def execute_action(self, action_type: str, params: dict) -> str:
        action = str(action_type or "").strip().lower()
        bridge_action = _ACTION_ALIASES.get(action)
        payload_params = dict(params) if isinstance(params, dict) else {}

        if bridge_action is None:
            return f"Unknown MomoAgent action: {action_type}"

        payload, error = self._invoke_bridge(bridge_action, payload_params)
        self._last_runtime = {
            "action_type": str(action_type or ""),
            "bridge_action": bridge_action,
            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
            "error": error or "",
            "payload": payload if isinstance(payload, dict) else {},
        }

        if error:
            return f"MomoAgent {bridge_action} failed: {error}"
        if not isinstance(payload, dict):
            return f"MomoAgent {bridge_action} failed: invalid bridge response"
        if not bool(payload.get("ok")):
            return f"MomoAgent {bridge_action} failed: {self._extract_error(payload)}"
        return self._format_success(bridge_action, payload)

    def get_scene(self) -> dict[str, dict]:
        scene = dict(self._scene)
        if self._last_runtime:
            scene["_momoagent_runtime"] = {
                "action_type": self._last_runtime.get("action_type", ""),
                "bridge_action": self._last_runtime.get("bridge_action", ""),
                "ok": bool(self._last_runtime.get("ok", False)),
                "error": str(self._last_runtime.get("error", "")),
            }
        return scene

    def connect(self) -> bool:
        payload, error = self._invoke_bridge("preflight", {"connect": True})
        self._last_runtime = {
            "action_type": "connect",
            "bridge_action": "preflight",
            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
            "error": error or "",
            "payload": payload if isinstance(payload, dict) else {},
        }
        return bool(payload and payload.get("ok")) and not error

    def disconnect(self) -> None:
        self._last_runtime = {}

    def is_connected(self) -> bool:
        payload, error = self._invoke_bridge("preflight", {"connect": False})
        return bool(payload and payload.get("ok")) and not error

    def health_check(self) -> bool:
        return self.is_connected()

    def get_runtime_state(self) -> dict[str, Any]:
        payload = self._last_runtime.get("payload") if isinstance(self._last_runtime, dict) else {}
        return {
            "driver": "momoagent",
            "ok": bool(self._last_runtime.get("ok", False)) if self._last_runtime else False,
            "error": str(self._last_runtime.get("error", "")) if self._last_runtime else "",
            "last_action": str(self._last_runtime.get("bridge_action", "")) if self._last_runtime else "",
            "state": payload.get("state") or payload.get("after") if isinstance(payload, dict) else {},
        }

    def close(self) -> None:
        self.disconnect()

    @staticmethod
    def _resolve_runtime_root(explicit_root: Any) -> Path:
        raw = explicit_root or os.environ.get("MOMOAGENT_RUNTIME_ROOT")
        return Path(raw).expanduser().resolve() if raw else _DEFAULT_RUNTIME_ROOT

    def _invoke_bridge(self, action: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if not self._bridge_script.exists():
            return None, f"bridge script not found: {self._bridge_script}"

        argv = [
            self._python_bin,
            "-u",
            str(self._bridge_script),
            str(action),
            "--params-json",
            json.dumps(dict(params or {}), ensure_ascii=False),
        ]
        timeout_s = self._resolve_timeout_s(action, params)

        try:
            proc = subprocess.run(
                argv,
                cwd=str(self._runtime_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None, f"timeout after {timeout_s}s"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

        payload = self._parse_payload(proc.stdout)
        if payload is None:
            detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
            return None, detail
        if proc.returncode != 0 and not bool(payload.get("ok")):
            return payload, self._extract_error(payload)
        return payload, None

    def _resolve_timeout_s(self, action: str, params: dict[str, Any]) -> float:
        raw = params.get("driver_timeout_s", params.get("timeout_s", self._default_timeout_s))
        try:
            value = float(raw)
        except Exception:
            value = self._default_timeout_s
        if action in {"pose", "cartesian_delta", "joints_target", "home"}:
            return max(value, 45.0)
        return max(value, 5.0)

    @staticmethod
    def _parse_payload(stdout_text: str) -> dict[str, Any] | None:
        text = (stdout_text or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        match = re.search(r"({[\s\S]*})\s*$", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(1))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _extract_error(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if isinstance(error, dict):
            message = error.get("message") or error.get("error")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return str(payload.get("error_type") or "unknown error")

    @staticmethod
    def _format_success(action: str, payload: dict[str, Any]) -> str:
        if action == "preflight":
            mode = "connected" if payload.get("connected") else "import-ready"
            return f"MomoAgent preflight succeeded ({mode})"
        if action == "state":
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            joints = state.get("joints_deg") if isinstance(state, dict) else {}
            return f"MomoAgent state read succeeded: joints={joints}"
        return f"MomoAgent {action} succeeded"
