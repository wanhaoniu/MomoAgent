from __future__ import annotations

import math
import os
import random
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

DEFAULT_SDK_REAL_CONFIG_PATH = (
    SDK_SRC / "soarmmoce_sdk" / "resources" / "configs" / "soarm_moce_serial.yaml"
)
DEFAULT_JOINT_NAMES = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]
CONTROL_MODE_NONE = "none"
CONTROL_MODE_FOLLOW = "follow"
CONTROL_MODE_IDLE_SCAN = "idle_scan"

from soarmmoce_sdk.runtime_compat import CompatibleRuntimeRobot as RuntimeRobot

from .agent_service import AgentService
from .errors import QuickControlError
from .face_follow_worker import (
    DEFAULT_PAN_JOINT as DEFAULT_FOLLOW_PAN_JOINT,
    DEFAULT_TILT_JOINT as DEFAULT_FOLLOW_TILT_JOINT,
    FaceFollowConfig,
    FaceFollowWorker,
    build_default_follow_payload,
)


@dataclass
class MotionConfig:
    speed_percent: int = 50
    coord_frame: str = "base"
    jog_mode: str = "step"
    step_dist_mm: float = 5.0
    step_angle_deg: float = 5.0


@dataclass
class IdleScanConfig:
    enabled: bool = False
    speed_percent: int = 25
    pan_range_deg: float = 10.0
    tilt_range_deg: float = 8.0
    move_duration_min_sec: float = 1.2
    move_duration_max_sec: float = 2.8
    dwell_sec_min: float = 0.8
    dwell_sec_max: float = 2.5
    phase: str = "none"
    phase_deadline_monotonic: float = 0.0
    anchor_pan_deg: Optional[float] = None
    anchor_tilt_deg: Optional[float] = None
    current_target_pan_deg: Optional[float] = None
    current_target_tilt_deg: Optional[float] = None


class QuickControlService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._robot: Optional[RuntimeRobot] = None
        self._agent = AgentService()
        env_sdk_config = str(os.getenv("SOARMMOCE_CONFIG", "")).strip()
        self._primary_config_path = env_sdk_config or (
            str(DEFAULT_SDK_REAL_CONFIG_PATH) if DEFAULT_SDK_REAL_CONFIG_PATH.exists() else ""
        )
        self._mode = "disconnected"
        self._transport_name = ""
        self._config_path = ""
        self._last_connect_error = ""
        self._motion = MotionConfig()
        self._control_mode = CONTROL_MODE_NONE
        self._control_error = ""
        self._follow_worker: FaceFollowWorker | None = None
        self._last_follow_payload: dict[str, Any] = build_default_follow_payload()
        self._idle_scan = IdleScanConfig()
        self._rng = random.Random()
        self._control_loop_stop = threading.Event()
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="QuickControlContinuousLoop",
            daemon=True,
        )
        self._control_thread.start()

    def close(self) -> None:
        self._control_loop_stop.set()
        try:
            self._control_thread.join(timeout=1.0)
        except Exception:
            pass
        self.disconnect()
        self._agent.close()

    def _build_robot(self, config_path: Optional[str]) -> RuntimeRobot:
        return RuntimeRobot.from_config(config_path) if config_path else RuntimeRobot()

    def _set_runtime(
        self,
        *,
        robot: Optional[RuntimeRobot] = None,
        mode: str = "disconnected",
        config_path: str = "",
        last_connect_error: str = "",
    ) -> None:
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

    def connect(
        self,
        *,
        prefer_real: bool = True,
        allow_sim_fallback: bool = True,
    ) -> dict[str, Any]:
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
                self._set_runtime(
                    mode="disconnected",
                    config_path=config_path,
                    last_connect_error=str(exc),
                )
                raise QuickControlError("CONNECT_FAILED", str(exc), 500) from exc

            self._robot = robot
            self._set_runtime(
                robot=robot,
                mode="connected",
                config_path=config_path,
                last_connect_error="",
            )
            self._control_error = ""
            return self._session_payload()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._disconnect_locked()
            return self._session_payload()

    def _disconnect_locked(self) -> None:
        self._deactivate_control_locked()
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

    def _update_motion(
        self,
        *,
        speed_percent: Optional[int] = None,
        coord_frame: Optional[str] = None,
        jog_mode: Optional[str] = None,
        step_dist_mm: Optional[float] = None,
        step_angle_deg: Optional[float] = None,
    ) -> None:
        if speed_percent is not None:
            self._motion.speed_percent = int(max(1, min(100, speed_percent)))
        if coord_frame is not None:
            self._motion.coord_frame = (
                "tool" if str(coord_frame).strip().lower() == "tool" else "base"
            )
        if jog_mode is not None:
            self._motion.jog_mode = (
                "continuous"
                if str(jog_mode).strip().lower() == "continuous"
                else "step"
            )
        if step_dist_mm is not None:
            self._motion.step_dist_mm = float(max(0.1, min(200.0, step_dist_mm)))
        if step_angle_deg is not None:
            self._motion.step_angle_deg = float(max(0.1, min(180.0, step_angle_deg)))

    def _duration_from_speed(
        self,
        *,
        kind: str,
        speed_percent: int,
        jog_mode: str = "step",
    ) -> float:
        speed_scale = max(0.1, float(speed_percent) / 100.0)
        if kind == "joint_step":
            return float(np.clip(0.20 / speed_scale, 0.08, 0.60))
        if kind == "home":
            return float(np.clip(1.8 / speed_scale, 0.5, 5.0))
        if kind == "continuous_joint":
            return float(np.clip(0.12 / speed_scale, 0.10, 0.35))
        if jog_mode == "continuous":
            return float(np.clip(0.15 / speed_scale, 0.08, 0.50))
        return float(np.clip(0.20 / speed_scale, 0.08, 0.60))

    def _read_robot_state_locked(self) -> Optional[Any]:
        robot = self._robot
        if robot is None or not getattr(robot, "connected", False):
            return None
        return robot.get_state()

    @staticmethod
    def _joint_names_from_state(state: Any) -> list[str]:
        names = list(getattr(state.joint_state, "names", []) or [])
        if names:
            return [str(name) for name in names]
        return list(DEFAULT_JOINT_NAMES[:-1])

    @staticmethod
    def _joint_index_by_name(names: list[str], joint_name: str) -> int:
        for index, name in enumerate(names):
            if str(name) == str(joint_name):
                return int(index)
        raise QuickControlError("INVALID_CONFIGURATION", f"Joint not found: {joint_name}", 500)

    def _follow_payload_locked(self) -> dict[str, Any]:
        worker = self._follow_worker
        if worker is not None:
            payload = worker.status_payload()
            self._last_follow_payload = dict(payload)
            return payload
        return dict(self._last_follow_payload)

    def _idle_scan_payload_locked(self) -> dict[str, Any]:
        phase = str(self._idle_scan.phase or "none")
        dwell_remaining_sec: Optional[float] = None
        if phase == "dwell" and self._idle_scan.phase_deadline_monotonic > 0.0:
            dwell_remaining_sec = max(
                0.0,
                float(self._idle_scan.phase_deadline_monotonic) - time.monotonic(),
            )
        return {
            "enabled": bool(self._idle_scan.enabled),
            "phase": phase,
            "speed_percent": int(self._idle_scan.speed_percent),
            "pan_range_deg": float(self._idle_scan.pan_range_deg),
            "tilt_range_deg": float(self._idle_scan.tilt_range_deg),
            "move_duration_min_sec": float(self._idle_scan.move_duration_min_sec),
            "move_duration_max_sec": float(self._idle_scan.move_duration_max_sec),
            "dwell_sec_min": float(self._idle_scan.dwell_sec_min),
            "dwell_sec_max": float(self._idle_scan.dwell_sec_max),
            "anchor_pan_deg": self._idle_scan.anchor_pan_deg,
            "anchor_tilt_deg": self._idle_scan.anchor_tilt_deg,
            "current_target_pan_deg": self._idle_scan.current_target_pan_deg,
            "current_target_tilt_deg": self._idle_scan.current_target_tilt_deg,
            "dwell_remaining_sec": dwell_remaining_sec,
        }

    def _deactivate_control_locked(self) -> None:
        self._control_mode = CONTROL_MODE_NONE
        if self._follow_worker is not None:
            self._last_follow_payload = self._follow_worker.status_payload()
            self._last_follow_payload["enabled"] = False
            self._last_follow_payload["running"] = False
            self._follow_worker.request_stop()
            self._follow_worker = None
        self._idle_scan.enabled = False
        self._idle_scan.phase = "none"
        self._idle_scan.phase_deadline_monotonic = 0.0
        self._idle_scan.current_target_pan_deg = None
        self._idle_scan.current_target_tilt_deg = None

    def _stop_for_manual_motion_locked(self) -> None:
        if self._control_mode != CONTROL_MODE_NONE:
            self._deactivate_control_locked()

    def robot_state_payload(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_robot_state_locked()
            follow_payload = self._follow_payload_locked()
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
                joint_names = self._joint_names_from_state(state)
                if joint_names:
                    names = joint_names
                values_rad = [
                    float(v)
                    for v in np.asarray(state.joint_state.q, dtype=float).reshape(-1).tolist()
                ]
                xyz_m = [
                    float(v)
                    for v in np.asarray(state.tcp_pose.xyz, dtype=float).reshape(3).tolist()
                ]
                rpy_rad = [
                    float(v)
                    for v in np.asarray(state.tcp_pose.rpy, dtype=float).reshape(3).tolist()
                ]
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

            control_error = str(self._control_error or "").strip()
            if self._control_mode == CONTROL_MODE_FOLLOW:
                control_error = str(follow_payload.get("last_error", "") or "").strip()

            return {
                "session": self._session_payload(),
                "robot": {
                    "state_text": state_text,
                    "status_light": status_light,
                },
                "control_mode": str(self._control_mode),
                "control_error": control_error,
                "motion": {
                    "speed_percent": int(self._motion.speed_percent),
                    "coord_frame": str(self._motion.coord_frame),
                    "jog_mode": str(self._motion.jog_mode),
                    "step_dist_mm": float(self._motion.step_dist_mm),
                    "step_angle_deg": float(self._motion.step_angle_deg),
                },
                "follow": follow_payload,
                "idle_scan": self._idle_scan_payload_locked(),
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

    def _move_named_joints_locked(
        self,
        *,
        pan_target_deg: Optional[float] = None,
        tilt_target_deg: Optional[float] = None,
        speed_percent: int,
        duration: Optional[float] = None,
    ) -> dict[str, Any]:
        robot = self._require_robot()
        state = robot.get_state()
        q_target = np.asarray(state.joint_state.q, dtype=float).copy()
        joint_names = self._joint_names_from_state(state)
        pan_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_PAN_JOINT)
        tilt_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_TILT_JOINT)
        if pan_target_deg is not None:
            lo, hi = robot.robot_model.joint_limits[pan_index]
            q_target[pan_index] = float(
                np.clip(math.radians(float(pan_target_deg)), float(lo), float(hi))
            )
        if tilt_target_deg is not None:
            lo, hi = robot.robot_model.joint_limits[tilt_index]
            q_target[tilt_index] = float(
                np.clip(math.radians(float(tilt_target_deg)), float(lo), float(hi))
            )
        move_duration = (
            float(duration)
            if duration is not None
            else self._duration_from_speed(kind="continuous_joint", speed_percent=speed_percent)
        )
        robot.move_joints(q_target, duration=move_duration, wait=False)
        return {
            "joint_names": joint_names,
            "target_q": q_target,
            "duration": move_duration,
        }

    def _control_loop(self) -> None:
        while not self._control_loop_stop.is_set():
            try:
                with self._lock:
                    if self._control_mode == CONTROL_MODE_IDLE_SCAN:
                        self._idle_scan_tick_locked()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._control_error = str(exc).strip() or "continuous controller failed"
            self._control_loop_stop.wait(0.10)

    def _idle_scan_tick_locked(self) -> None:
        if not self._idle_scan.enabled:
            return
        robot = self._robot
        if robot is None or not getattr(robot, "connected", False):
            return
        now = time.monotonic()
        phase = str(self._idle_scan.phase or "none")
        if phase == "moving":
            if now < float(self._idle_scan.phase_deadline_monotonic):
                return
            dwell_duration = self._rng.uniform(
                float(self._idle_scan.dwell_sec_min),
                float(self._idle_scan.dwell_sec_max),
            )
            self._idle_scan.phase = "dwell"
            self._idle_scan.phase_deadline_monotonic = now + float(dwell_duration)
            self._control_error = ""
            return
        if phase == "dwell" and now < float(self._idle_scan.phase_deadline_monotonic):
            return

        state = robot.get_state()
        joint_names = self._joint_names_from_state(state)
        pan_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_PAN_JOINT)
        tilt_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_TILT_JOINT)
        q_current = np.asarray(state.joint_state.q, dtype=float).copy()
        current_pan_deg = math.degrees(float(q_current[pan_index]))
        current_tilt_deg = math.degrees(float(q_current[tilt_index]))
        if self._idle_scan.anchor_pan_deg is None:
            self._idle_scan.anchor_pan_deg = current_pan_deg
        if self._idle_scan.anchor_tilt_deg is None:
            self._idle_scan.anchor_tilt_deg = current_tilt_deg

        target_pan_deg = float(self._idle_scan.anchor_pan_deg) + self._rng.uniform(
            -float(self._idle_scan.pan_range_deg),
            float(self._idle_scan.pan_range_deg),
        )
        target_tilt_deg = float(self._idle_scan.anchor_tilt_deg) + self._rng.uniform(
            -float(self._idle_scan.tilt_range_deg),
            float(self._idle_scan.tilt_range_deg),
        )
        duration = self._rng.uniform(
            float(self._idle_scan.move_duration_min_sec),
            float(self._idle_scan.move_duration_max_sec),
        )
        self._move_named_joints_locked(
            pan_target_deg=target_pan_deg,
            tilt_target_deg=target_tilt_deg,
            speed_percent=int(self._idle_scan.speed_percent),
            duration=duration,
        )
        self._idle_scan.phase = "moving"
        self._idle_scan.phase_deadline_monotonic = now + float(duration) + 0.05
        self._idle_scan.current_target_pan_deg = float(target_pan_deg)
        self._idle_scan.current_target_tilt_deg = float(target_tilt_deg)
        self._control_error = ""

    def joint_step(self, *, joint_index: int, delta_deg: float, speed_percent: int) -> dict[str, Any]:
        with self._lock:
            self._stop_for_manual_motion_locked()
            self._update_motion(speed_percent=speed_percent, step_angle_deg=abs(delta_deg))
            robot = self._require_robot()
            state = robot.get_state()
            q_target = np.asarray(state.joint_state.q, dtype=float).copy()
            joint_names = list(
                getattr(state.joint_state, "names", [])
                or getattr(robot.robot_model, "joint_names", [])
                or DEFAULT_JOINT_NAMES
            )
            if joint_index < 0:
                raise QuickControlError(
                    "INVALID_ARGUMENT",
                    f"joint_index out of range: {joint_index}",
                )
            if joint_index >= q_target.shape[0]:
                if (
                    joint_index == len(DEFAULT_JOINT_NAMES) - 1
                    and bool(getattr(state.gripper_state, "available", False))
                ):
                    current_ratio = getattr(state.gripper_state, "open_ratio", None)
                    if current_ratio is None:
                        current_ratio = 0.5
                    ratio_step = 0.05 if float(delta_deg) >= 0.0 else -0.05
                    robot.set_gripper(
                        open_ratio=float(
                            np.clip(float(current_ratio) + ratio_step, 0.0, 1.0)
                        ),
                        wait=False,
                    )
                    return {
                        "joint_index": int(joint_index),
                        "joint_name": "gripper",
                        "delta_deg": float(delta_deg),
                        "accepted": True,
                    }
                raise QuickControlError(
                    "INVALID_ARGUMENT",
                    f"joint_index out of range: {joint_index}",
                )
            lo, hi = robot.robot_model.joint_limits[joint_index]
            q_target[joint_index] = float(
                np.clip(
                    q_target[joint_index] + math.radians(float(delta_deg)),
                    float(lo),
                    float(hi),
                )
            )
            duration = self._duration_from_speed(kind="joint_step", speed_percent=speed_percent)
            robot.move_joints(q_target, duration=duration, wait=False)
            return {
                "joint_index": int(joint_index),
                "joint_name": (
                    str(joint_names[joint_index])
                    if joint_index < len(joint_names)
                    else f"joint_{joint_index}"
                ),
                "delta_deg": float(delta_deg),
                "accepted": True,
            }

    def cartesian_jog(
        self,
        *,
        axis: str,
        coord_frame: str,
        jog_mode: str,
        step_dist_mm: float,
        step_angle_deg: float,
        speed_percent: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._stop_for_manual_motion_locked()
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
                raise QuickControlError(
                    "INVALID_ARGUMENT",
                    f"Unsupported cartesian axis: {axis}",
                    400,
                )

            key, sign = axis_map[axis_norm]
            step_value = trans_step_m if key in {"dx", "dy", "dz"} else rot_step_rad
            delta_kwargs[key] = float(sign) * float(step_value)
            duration = self._duration_from_speed(
                kind="cartesian",
                speed_percent=speed_percent,
                jog_mode=jog_mode,
            )
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
                "coord_frame": (
                    "tool" if str(coord_frame).strip().lower() == "tool" else "base"
                ),
                "jog_mode": (
                    "continuous"
                    if str(jog_mode).strip().lower() == "continuous"
                    else "step"
                ),
                "step_dist_mm": float(step_dist_mm),
                "step_angle_deg": float(step_angle_deg),
                "accepted": True,
            }

    def home(self, *, source: str, speed_percent: int) -> dict[str, Any]:
        with self._lock:
            self._stop_for_manual_motion_locked()
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
            self._deactivate_control_locked()
            robot = self._robot
            if robot is not None and getattr(robot, "connected", False):
                robot.stop()
            return {"stopped": True}

    def follow_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "control_mode": str(self._control_mode),
                "follow": self._follow_payload_locked(),
            }

    def follow_start(
        self,
        *,
        target_kind: str,
        latest_url: str,
        poll_interval: float,
        http_timeout: float,
        move_duration: float,
        pan_joint: str,
        tilt_joint: str,
        pan_sign: float,
        tilt_sign: float,
        pan_gain: float,
        tilt_gain: float,
        pan_dead_zone: float,
        tilt_dead_zone: float,
        pan_resume_zone: float,
        tilt_resume_zone: float,
        min_pan_step: float,
        min_tilt_step: float,
        pan_min_step_zone: float,
        tilt_min_step_zone: float,
        max_pan_step: float,
        max_tilt_step: float,
        command_mode: str,
        limit_margin_raw: int,
        stiction_eps_deg: float,
        stiction_frames: int,
        pan_breakaway_step: float,
        pan_breakaway_step_pos: float | None,
        pan_breakaway_step_neg: float | None,
        pan_negative_scale: float,
        tilt_breakaway_step: float,
    ) -> dict[str, Any]:
        with self._lock:
            robot = self._require_robot()
            self._deactivate_control_locked()
            worker = FaceFollowWorker(
                robot=robot,
                robot_lock=self._lock,
                config=FaceFollowConfig(
                    target_kind=str(target_kind or "face"),
                    latest_url=str(latest_url or "").strip() or build_default_follow_payload()["latest_url"],
                    poll_interval=float(max(0.01, poll_interval)),
                    http_timeout=float(max(0.1, http_timeout)),
                    move_duration=float(max(0.01, move_duration)),
                    pan_joint=str(pan_joint or DEFAULT_FOLLOW_PAN_JOINT),
                    tilt_joint=str(tilt_joint or DEFAULT_FOLLOW_TILT_JOINT),
                    pan_sign=float(pan_sign),
                    tilt_sign=float(tilt_sign),
                    pan_gain=float(pan_gain),
                    tilt_gain=float(tilt_gain),
                    pan_dead_zone=float(max(0.0, pan_dead_zone)),
                    tilt_dead_zone=float(max(0.0, tilt_dead_zone)),
                    pan_resume_zone=float(max(0.0, pan_resume_zone)),
                    tilt_resume_zone=float(max(0.0, tilt_resume_zone)),
                    min_pan_step=float(max(0.0, min_pan_step)),
                    min_tilt_step=float(max(0.0, min_tilt_step)),
                    pan_min_step_zone=float(max(0.0, pan_min_step_zone)),
                    tilt_min_step_zone=float(max(0.0, tilt_min_step_zone)),
                    max_pan_step=float(max(0.0, max_pan_step)),
                    max_tilt_step=float(max(0.0, max_tilt_step)),
                    command_mode=(
                        "settle"
                        if str(command_mode).strip().lower() == "settle"
                        else "stream"
                    ),
                    limit_margin_raw=int(max(0, limit_margin_raw)),
                    stiction_eps_deg=float(max(0.0, stiction_eps_deg)),
                    stiction_frames=int(max(1, stiction_frames)),
                    pan_breakaway_step=float(max(0.0, pan_breakaway_step)),
                    pan_breakaway_step_pos=(
                        None if pan_breakaway_step_pos is None else float(max(0.0, pan_breakaway_step_pos))
                    ),
                    pan_breakaway_step_neg=(
                        float(max(0.0, pan_breakaway_step_neg))
                        if pan_breakaway_step_neg is not None
                        else float(max(0.0, pan_breakaway_step))
                    ),
                    pan_negative_scale=float(max(1.0, pan_negative_scale)),
                    tilt_breakaway_step=float(max(0.0, tilt_breakaway_step)),
                ),
            )
            worker.start()
            self._follow_worker = worker
            self._last_follow_payload = worker.status_payload()
            self._last_follow_payload.update(
                {
                    "enabled": True,
                    "running": True,
                }
            )
            self._control_mode = CONTROL_MODE_FOLLOW
            self._control_error = ""
            return self.follow_status()

    def follow_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._control_mode == CONTROL_MODE_FOLLOW or self._follow_worker is not None:
                self._deactivate_control_locked()
            return self.follow_status()

    def idle_scan_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "control_mode": str(self._control_mode),
                "idle_scan": self._idle_scan_payload_locked(),
            }

    def idle_scan_start(
        self,
        *,
        speed_percent: int,
        pan_range_deg: float,
        tilt_range_deg: float,
        move_duration_min_sec: float,
        move_duration_max_sec: float,
        dwell_sec_min: float,
        dwell_sec_max: float,
    ) -> dict[str, Any]:
        with self._lock:
            robot = self._require_robot()
            state = robot.get_state()
            joint_names = self._joint_names_from_state(state)
            pan_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_PAN_JOINT)
            tilt_index = self._joint_index_by_name(joint_names, DEFAULT_FOLLOW_TILT_JOINT)
            q_current = np.asarray(state.joint_state.q, dtype=float).copy()
            self._deactivate_control_locked()
            move_min = float(max(0.2, min(move_duration_min_sec, move_duration_max_sec)))
            move_max = float(max(move_min, move_duration_max_sec))
            dwell_min = float(max(0.0, min(dwell_sec_min, dwell_sec_max)))
            dwell_max = float(max(dwell_min, dwell_sec_max))
            self._idle_scan = IdleScanConfig(
                enabled=True,
                speed_percent=int(max(1, min(100, speed_percent))),
                pan_range_deg=float(np.clip(pan_range_deg, 1.0, 45.0)),
                tilt_range_deg=float(np.clip(tilt_range_deg, 1.0, 30.0)),
                move_duration_min_sec=move_min,
                move_duration_max_sec=move_max,
                dwell_sec_min=dwell_min,
                dwell_sec_max=dwell_max,
                phase="dwell",
                phase_deadline_monotonic=time.monotonic(),
                anchor_pan_deg=math.degrees(float(q_current[pan_index])),
                anchor_tilt_deg=math.degrees(float(q_current[tilt_index])),
            )
            self._control_mode = CONTROL_MODE_IDLE_SCAN
            self._control_error = ""
            return self.idle_scan_status()

    def idle_scan_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._control_mode == CONTROL_MODE_IDLE_SCAN or self._idle_scan.enabled:
                self._control_mode = CONTROL_MODE_NONE
                self._idle_scan.enabled = False
                self._idle_scan.phase = "none"
                self._idle_scan.phase_deadline_monotonic = 0.0
                self._idle_scan.current_target_pan_deg = None
                self._idle_scan.current_target_tilt_deg = None
            return self.idle_scan_status()

    def agent_status(self) -> dict[str, Any]:
        return self._agent.status_payload()

    def agent_last_turn(self) -> dict[str, Any]:
        return self._agent.last_turn_payload()

    def agent_ask(self, *, message: str) -> dict[str, Any]:
        return self._agent.ask(message)

    def agent_warmup(self, *, prompt: str = "请只回复“就绪”。") -> dict[str, Any]:
        return self._agent.warmup(prompt=prompt)

    def agent_reset_session(self) -> dict[str, Any]:
        return self._agent.reset_session()

    def agent_tts_status(self) -> dict[str, Any]:
        return self._agent.tts_status_payload()

    def agent_build_tts_stream_spec(self, *, text: str) -> dict[str, Any]:
        return self._agent.build_tts_stream_spec(text)

    def agent_set_last_turn_tts_summary(self, *, summary: dict[str, Any]) -> dict[str, Any]:
        return self._agent.set_last_turn_tts_summary(summary)
