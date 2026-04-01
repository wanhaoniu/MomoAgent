from __future__ import annotations

import math
import os
import sys
import threading
import time
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
DEFAULT_SDK_SIM_CONFIG_PATH = SDK_SRC / "soarmmoce_sdk" / "resources" / "configs" / "soarm_moce.yaml"
DEFAULT_JOINT_NAMES = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

from soarmmoce_sdk import Robot as RuntimeRobot
from soarmmoce_sdk.kinematics.fk import matrix_to_rpy
from soarmmoce_sdk.kinematics.frames import rpy_to_matrix


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
        self._transport_override = str(os.getenv("SOARMMOCE_TRANSPORT", "")).strip().lower() or None
        env_sdk_config = str(os.getenv("SOARMMOCE_CONFIG", "")).strip()
        self._primary_config_path = env_sdk_config or (str(DEFAULT_SDK_REAL_CONFIG_PATH) if DEFAULT_SDK_REAL_CONFIG_PATH.exists() else "")
        self._sim_config_path = str(DEFAULT_SDK_SIM_CONFIG_PATH) if DEFAULT_SDK_SIM_CONFIG_PATH.exists() else ""
        self._mode = "disconnected"
        self._simulation_fallback = False
        self._transport_name = ""
        self._config_path = ""
        self._last_connect_error = ""
        self._motion = MotionConfig()

    def close(self) -> None:
        self.disconnect()

    def _build_robot(self, config_path: Optional[str], transport_override: Optional[str] = None) -> RuntimeRobot:
        robot = RuntimeRobot.from_config(config_path) if config_path else RuntimeRobot()
        if transport_override in ("mock", "tcp", "serial"):
            robot.config.setdefault("transport", {})["type"] = str(transport_override)
        return robot

    def _set_runtime(self, *, robot: Optional[RuntimeRobot] = None, mode: str = "disconnected", simulation_fallback: bool = False, config_path: str = "", last_connect_error: str = "") -> None:
        self._mode = str(mode or "disconnected")
        self._simulation_fallback = bool(simulation_fallback)
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
            "simulation_fallback": bool(self._simulation_fallback),
            "transport": self._transport_name,
            "config_path": self._config_path,
        }

    def session_status(self) -> dict[str, Any]:
        with self._lock:
            return self._session_payload()

    def connect(self, *, prefer_real: bool = True, allow_sim_fallback: bool = True) -> dict[str, Any]:
        with self._lock:
            if self._robot is not None and getattr(self._robot, "connected", False):
                return self._session_payload()

            self._disconnect_locked()
            explicit_override = self._transport_override in ("mock", "tcp", "serial")
            candidates: list[tuple[str, str, Optional[str], bool]] = []

            if explicit_override:
                cfg = self._primary_config_path or self._sim_config_path
                if not cfg:
                    raise QuickControlError("CONNECT_FAILED", "No SDK config available", 500)
                candidates.append((cfg, str(self._transport_override), False, False))
            else:
                if prefer_real and self._primary_config_path:
                    candidates.append((self._primary_config_path, "", False, False))
                if self._sim_config_path and ((not prefer_real) or allow_sim_fallback):
                    candidates.append((self._sim_config_path, "mock", True, bool(prefer_real and allow_sim_fallback and self._primary_config_path)))

            seen: set[tuple[str, str]] = set()
            deduped: list[tuple[str, str, bool, bool]] = []
            for cfg, override, is_sim, is_fallback in candidates:
                key = (os.path.normpath(cfg), override)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append((cfg, override, is_sim, is_fallback))

            last_exc: Optional[Exception] = None
            for cfg, override, is_sim, is_fallback in deduped:
                try:
                    robot = self._build_robot(cfg, override or None)
                    robot.connect()
                    mode = "simulation" if is_sim or "mock" in self._transport_name_from_robot(robot).lower() else "connected"
                    self._robot = robot
                    self._set_runtime(
                        robot=robot,
                        mode=mode,
                        simulation_fallback=is_fallback,
                        config_path=cfg,
                        last_connect_error="" if mode == "connected" else (str(last_exc) if is_fallback and last_exc else ""),
                    )
                    return self._session_payload()
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    continue

            self._set_runtime(mode="disconnected", last_connect_error=str(last_exc or "connect failed"))
            raise QuickControlError("CONNECT_FAILED", str(last_exc or "connect failed"), 500)

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

    @staticmethod
    def _rotation_from_rpy_delta(delta_rot_local: np.ndarray) -> np.ndarray:
        rx, ry, rz = [float(x) for x in np.asarray(delta_rot_local, dtype=float).reshape(3)]
        return rpy_to_matrix(np.array([rx, ry, rz], dtype=float))

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
            if joint_index < 0 or joint_index >= q_target.shape[0]:
                raise QuickControlError("INVALID_ARGUMENT", f"joint_index out of range: {joint_index}")
            joint_names = list(getattr(state.joint_state, "names", []) or getattr(robot.robot_model, "joint_names", []) or DEFAULT_JOINT_NAMES)
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
        key_norm = str(axis).strip().upper()
        trans_map = {
            "+X": np.array([1.0, 0.0, 0.0], dtype=float),
            "-X": np.array([-1.0, 0.0, 0.0], dtype=float),
            "+Y": np.array([0.0, 1.0, 0.0], dtype=float),
            "-Y": np.array([0.0, -1.0, 0.0], dtype=float),
            "+Z": np.array([0.0, 0.0, 1.0], dtype=float),
            "-Z": np.array([0.0, 0.0, -1.0], dtype=float),
        }
        rot_map = {
            "+RX": np.array([1.0, 0.0, 0.0], dtype=float),
            "-RX": np.array([-1.0, 0.0, 0.0], dtype=float),
            "+RY": np.array([0.0, 1.0, 0.0], dtype=float),
            "-RY": np.array([0.0, -1.0, 0.0], dtype=float),
            "+RZ": np.array([0.0, 0.0, 1.0], dtype=float),
            "-RZ": np.array([0.0, 0.0, -1.0], dtype=float),
        }
        if key_norm not in trans_map and key_norm not in rot_map:
            raise QuickControlError("INVALID_ARGUMENT", f"Unsupported axis: {axis}")

        with self._lock:
            self._update_motion(
                speed_percent=speed_percent,
                coord_frame=coord_frame,
                jog_mode=jog_mode,
                step_dist_mm=step_dist_mm,
                step_angle_deg=step_angle_deg,
            )
            robot = self._require_robot()
            state = robot.get_state()
            xyz_now = np.asarray(state.tcp_pose.xyz, dtype=float).reshape(3)
            rpy_now = np.asarray(state.tcp_pose.rpy, dtype=float).reshape(3)
            R_now = rpy_to_matrix(rpy_now)
            delta_pos_local = np.zeros(3, dtype=float)
            delta_rot_local = np.zeros(3, dtype=float)
            if key_norm in trans_map:
                delta_pos_local = trans_map[key_norm] * (float(step_dist_mm) / 1000.0)
            else:
                delta_rot_local = rot_map[key_norm] * math.radians(float(step_angle_deg))

            use_tool = str(coord_frame).strip().lower() == "tool"
            if use_tool:
                target_xyz = xyz_now + (R_now @ delta_pos_local)
                R_target = R_now @ self._rotation_from_rpy_delta(delta_rot_local)
            else:
                target_xyz = xyz_now + delta_pos_local
                R_target = self._rotation_from_rpy_delta(delta_rot_local) @ R_now

            duration = self._duration_from_speed(kind="cartesian_jog", speed_percent=speed_percent, jog_mode=jog_mode)
            if key_norm in trans_map:
                robot.move_pose(xyz=target_xyz, rpy=None, duration=duration, wait=False)
            else:
                robot.move_pose(xyz=target_xyz, rpy=matrix_to_rpy(R_target), duration=duration, wait=False)
            return {
                "axis": key_norm,
                "coord_frame": "tool" if use_tool else "base",
                "jog_mode": "continuous" if str(jog_mode).strip().lower() == "continuous" else "step",
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
