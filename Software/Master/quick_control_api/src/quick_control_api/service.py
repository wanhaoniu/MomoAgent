from __future__ import annotations

import math
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[5]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    sdk_src_norm = os.path.normpath(sdk_src_str)
    sys.path[:] = [
        path
        for path in sys.path
        if os.path.normpath(path or os.curdir) != sdk_src_norm
    ]
    sys.path.insert(0, sdk_src_str)

DEFAULT_SDK_REAL_CONFIG_PATH = SDK_SRC / "soarmmoce_sdk" / "resources" / "configs" / "soarm_moce_serial.yaml"
DEFAULT_JOINT_NAMES = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

from soarmmoce_sdk.runtime_compat import CompatibleRuntimeRobot as RuntimeRobot


class QuickControlError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = str(code or "INTERNAL_ERROR")
        self.message = str(message or code or "error")
        self.status_code = int(status_code)


@dataclass
class MotionConfig:
    speed_percent: int = 50
    coord_frame: str = "base"
    jog_mode: str = "step"
    step_dist_mm: float = 5.0
    step_angle_deg: float = 5.0


class QuickControlService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._robot: Optional[RuntimeRobot] = None
        env_sdk_config = str(os.getenv("SOARMMOCE_CONFIG", "")).strip()
        self._primary_config_path = env_sdk_config or (str(DEFAULT_SDK_REAL_CONFIG_PATH) if DEFAULT_SDK_REAL_CONFIG_PATH.exists() else "")
        self._mode = "disconnected"
        self._transport_name = ""
        self._config_path = ""
        self._last_connect_error = ""
        self._motion = MotionConfig()

    def close(self) -> None:
        self.disconnect()

    def _build_robot(self, config_path: Optional[str]) -> RuntimeRobot:
        return RuntimeRobot.from_config(config_path) if config_path else RuntimeRobot()

    def _set_runtime(self, *, robot: Optional[RuntimeRobot] = None, mode: str = "disconnected", config_path: str = "", last_connect_error: str = "") -> None:
        self._mode = str(mode or "disconnected")
        self._config_path = str(config_path or "")
        self._last_connect_error = str(last_connect_error or "")
        if robot is None:
            self._transport_name = ""
        else:
            transport = getattr(robot, "_transport", None)
            self._transport_name = type(transport).__name__ if transport is not None else ""

    def _session_payload(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "connected": self._mode in ("connected", "simulation"),
            "simulation_fallback": False,
            "transport": self._transport_name,
            "config_path": self._config_path,
        }

    def session_status(self) -> dict[str, Any]:
        with self._lock:
            return self._session_payload()

    def connect(self, *, prefer_real: bool = True, allow_sim_fallback: bool = True) -> dict[str, Any]:
        with self._lock:
            del prefer_real
            del allow_sim_fallback
            if self._robot is not None and getattr(self._robot, "connected", False):
                return self._session_payload()

            self._disconnect_locked()
            config_path = self._primary_config_path
            if not config_path:
                raise QuickControlError("CONNECT_FAILED", "No SDK config available", 500)

            try:
                robot = self._build_robot(config_path)
                try:
                    robot.connect(passive=True)
                except TypeError:
                    robot.connect()
            except Exception as exc:  # noqa: BLE001
                self._set_runtime(mode="disconnected", config_path=config_path, last_connect_error=str(exc))
                raise QuickControlError("CONNECT_FAILED", str(exc), 500) from exc

            self._robot = robot
            self._set_runtime(
                robot=robot,
                mode="connected",
                config_path=config_path,
                last_connect_error="",
            )
            return self._session_payload()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._disconnect_locked()
            return self._session_payload()

    def _disconnect_locked(self) -> None:
        if self._robot is not None:
            try:
                self._robot.disconnect()
            except Exception:
                pass
        self._robot = None
        self._set_runtime(mode="disconnected")

    def _require_robot(self) -> RuntimeRobot:
        robot = self._robot
        if robot is None or not getattr(robot, "connected", False):
            raise QuickControlError("NOT_CONNECTED", "Robot is not connected", 409)
        return robot

    @staticmethod
    def _transport_name_from_robot(robot: RuntimeRobot) -> str:
        transport_name = getattr(robot, "transport_name", None)
        if isinstance(transport_name, str) and transport_name.strip():
            return transport_name
        transport = getattr(robot, "_transport", None)
        return type(transport).__name__ if transport is not None else ""

    def _update_motion(self, *, speed_percent: Optional[int] = None, coord_frame: Optional[str] = None, jog_mode: Optional[str] = None, step_dist_mm: Optional[float] = None, step_angle_deg: Optional[float] = None) -> None:
        if speed_percent is not None:
            self._motion.speed_percent = int(max(1, min(100, speed_percent)))
        if coord_frame is not None:
            self._motion.coord_frame = "tool" if str(coord_frame).strip().lower() == "tool" else "base"
        if jog_mode is not None:
            self._motion.jog_mode = "continuous" if str(jog_mode).strip().lower() == "continuous" else "step"
        if step_dist_mm is not None:
            self._motion.step_dist_mm = float(max(0.1, min(200.0, step_dist_mm)))
        if step_angle_deg is not None:
            self._motion.step_angle_deg = float(max(0.1, min(180.0, step_angle_deg)))

    def _duration_from_speed(self, *, kind: str, speed_percent: int, jog_mode: str = "step") -> float:
        speed_scale = max(0.1, float(speed_percent) / 100.0)
        if kind == "joint_step":
            return float(np.clip(0.20 / speed_scale, 0.08, 0.60))
        if kind == "home":
            return float(np.clip(1.8 / speed_scale, 0.5, 5.0))
        if jog_mode == "continuous":
            return float(np.clip(0.15 / speed_scale, 0.08, 0.50))
        return float(np.clip(0.20 / speed_scale, 0.08, 0.60))

    def _read_robot_state_locked(self) -> Optional[Any]:
        robot = self._robot
        if robot is None or not getattr(robot, "connected", False):
            return None
        return robot.get_state()

    def robot_state_payload(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_robot_state_locked()
            connected = state is not None and self._mode in ("connected", "simulation")
            status_light = "normal" if connected else "warning"
            state_text = "Normal" if connected else "Warning"
            names = list(DEFAULT_JOINT_NAMES)
            values_rad: list[float] = []
            xyz_m: list[float] = []
            rpy_rad: list[float] = []
            gripper = {
                "available": False,
                "open_ratio": None,
                "moving": None,
            }
            permissions = {
                "allow_motion": False,
                "allow_gripper": False,
                "allow_home": False,
                "allow_stop": False,
            }
            if state is not None:
                joint_names = list(getattr(state.joint_state, "names", []) or [])
                if joint_names:
                    names = [str(name) for name in joint_names]
                values_rad = [float(v) for v in np.asarray(state.joint_state.q, dtype=float).reshape(-1).tolist()]
                xyz_m = [float(v) for v in np.asarray(state.tcp_pose.xyz, dtype=float).reshape(3).tolist()]
                rpy_rad = [float(v) for v in np.asarray(state.tcp_pose.rpy, dtype=float).reshape(3).tolist()]
                g = getattr(state, "gripper_state", None)
                if g is not None:
                    gripper = {
                        "available": bool(getattr(g, "available", False)),
                        "open_ratio": getattr(g, "open_ratio", None),
                        "moving": getattr(g, "moving", None),
                    }
                p = getattr(state, "permissions", None)
                if p is not None:
                    permissions = {
                        "allow_motion": bool(getattr(p, "allow_motion", False)),
                        "allow_gripper": bool(getattr(p, "allow_gripper", False)),
                        "allow_home": bool(getattr(p, "allow_home", False)),
                        "allow_stop": bool(getattr(p, "allow_stop", False)),
                    }

            return {
                "session": self._session_payload(),
                "robot": {
                    "state_text": state_text,
                    "status_light": status_light,
                },
                "motion": {
                    "speed_percent": int(self._motion.speed_percent),
                    "coord_frame": str(self._motion.coord_frame),
                    "jog_mode": str(self._motion.jog_mode),
                    "step_dist_mm": float(self._motion.step_dist_mm),
                    "step_angle_deg": float(self._motion.step_angle_deg),
                },
                "joint_state": {
                    "names": names,
                    "values_rad": values_rad,
                },
                "tcp_pose": {
                    "xyz_m": xyz_m,
                    "rpy_rad": rpy_rad,
                },
                "gripper": gripper,
                "permissions": permissions,
                "rtt_text": "--",
            }

    def joint_step(self, *, joint_index: int, delta_deg: float, speed_percent: int) -> dict[str, Any]:
        with self._lock:
            self._update_motion(speed_percent=speed_percent, step_angle_deg=abs(delta_deg))
            robot = self._require_robot()
            state = robot.get_state()
            q_target = np.asarray(state.joint_state.q, dtype=float).copy()
            joint_names = list(getattr(state.joint_state, "names", []) or getattr(robot.robot_model, "joint_names", []) or DEFAULT_JOINT_NAMES)
            if joint_index < 0:
                raise QuickControlError("INVALID_ARGUMENT", f"joint_index out of range: {joint_index}")
            if joint_index >= q_target.shape[0]:
                if joint_index == len(DEFAULT_JOINT_NAMES) - 1 and bool(getattr(state.gripper_state, "available", False)):
                    current_ratio = getattr(state.gripper_state, "open_ratio", None)
                    if current_ratio is None:
                        current_ratio = 0.5
                    ratio_step = 0.05 if float(delta_deg) >= 0.0 else -0.05
                    robot.set_gripper(
                        open_ratio=float(np.clip(float(current_ratio) + ratio_step, 0.0, 1.0)),
                        wait=False,
                    )
                    return {
                        "joint_index": int(joint_index),
                        "joint_name": "gripper",
                        "delta_deg": float(delta_deg),
                        "accepted": True,
                    }
                raise QuickControlError("INVALID_ARGUMENT", f"joint_index out of range: {joint_index}")
            lo, hi = robot.robot_model.joint_limits[joint_index]
            q_target[joint_index] = float(np.clip(q_target[joint_index] + math.radians(float(delta_deg)), float(lo), float(hi)))
            duration = self._duration_from_speed(kind="joint_step", speed_percent=speed_percent)
            robot.move_joints(q_target, duration=duration, wait=False)
            return {
                "joint_index": int(joint_index),
                "joint_name": str(joint_names[joint_index]) if joint_index < len(joint_names) else f"joint_{joint_index}",
                "delta_deg": float(delta_deg),
                "accepted": True,
            }

    def cartesian_jog(self, *, axis: str, coord_frame: str, jog_mode: str, step_dist_mm: float, step_angle_deg: float, speed_percent: int) -> dict[str, Any]:
        with self._lock:
            self._update_motion(
                speed_percent=speed_percent,
                coord_frame=coord_frame,
                jog_mode=jog_mode,
                step_dist_mm=step_dist_mm,
                step_angle_deg=step_angle_deg,
            )
            robot = self._require_robot()
            axis_norm = str(axis or "").strip().upper()
            trans_step_m = float(step_dist_mm) / 1000.0
            rot_step_rad = math.radians(float(step_angle_deg))
            delta_kwargs = {
                "dx": 0.0,
                "dy": 0.0,
                "dz": 0.0,
                "drx": 0.0,
                "dry": 0.0,
                "drz": 0.0,
            }
            axis_map = {
                "+X": ("dx", 1.0),
                "X": ("dx", 1.0),
                "-X": ("dx", -1.0),
                "+Y": ("dy", 1.0),
                "Y": ("dy", 1.0),
                "-Y": ("dy", -1.0),
                "+Z": ("dz", 1.0),
                "Z": ("dz", 1.0),
                "-Z": ("dz", -1.0),
                "+RX": ("drx", 1.0),
                "RX": ("drx", 1.0),
                "-RX": ("drx", -1.0),
                "+RY": ("dry", 1.0),
                "RY": ("dry", 1.0),
                "-RY": ("dry", -1.0),
                "+RZ": ("drz", 1.0),
                "RZ": ("drz", 1.0),
                "-RZ": ("drz", -1.0),
            }
            if axis_norm not in axis_map:
                raise QuickControlError("INVALID_ARGUMENT", f"Unsupported cartesian axis: {axis}", 400)

            key, sign = axis_map[axis_norm]
            step_value = trans_step_m if key in {"dx", "dy", "dz"} else rot_step_rad
            delta_kwargs[key] = float(sign) * float(step_value)
            duration = self._duration_from_speed(kind="cartesian", speed_percent=speed_percent, jog_mode=jog_mode)
            try:
                robot.move_delta(
                    frame=str(coord_frame or "base"),
                    duration=duration,
                    wait=False,
                    **delta_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                raise QuickControlError("CARTESIAN_FAILED", str(exc), 500) from exc

            return {
                "axis": axis_norm,
                "coord_frame": "tool" if str(coord_frame).strip().lower() == "tool" else "base",
                "jog_mode": "continuous" if str(jog_mode).strip().lower() == "continuous" else "step",
                "step_dist_mm": float(step_dist_mm),
                "step_angle_deg": float(step_angle_deg),
                "accepted": True,
            }

    def home(self, *, source: str, speed_percent: int) -> dict[str, Any]:
        with self._lock:
            self._update_motion(speed_percent=speed_percent)
            robot = self._require_robot()
            duration = self._duration_from_speed(kind="home", speed_percent=speed_percent)
            robot.home(duration=duration, wait=False)
            return {
                "source": str(source or "home"),
                "accepted": True,
            }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            robot = self._require_robot()
            robot.stop()
            return {"stopped": True}
