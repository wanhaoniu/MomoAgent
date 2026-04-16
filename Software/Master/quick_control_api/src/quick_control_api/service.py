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
CONTROL_MODE_ATTENTION = "attention"
CONTROL_MODE_IDLE_SCAN = "idle_scan"
CONTROL_MODE_HAIGUITANG = "haiguitang"

from soarmmoce_sdk.runtime_compat import CompatibleRuntimeRobot as RuntimeRobot

from .agent_service import AgentService
from .attention_worker import AttentionConfig, AttentionWorker, build_default_attention_payload
from .errors import QuickControlError
from .face_follow_worker import (
    DEFAULT_PAN_JOINT as DEFAULT_FOLLOW_PAN_JOINT,
    DEFAULT_TILT_JOINT as DEFAULT_FOLLOW_TILT_JOINT,
    FaceFollowConfig,
    FaceFollowWorker,
)
from .haiguitang_worker import (
    HaiGuiTangConfig,
    HaiGuiTangWorker,
    build_default_haiguitang_payload,
)
from .idle_scan_worker import IdleScanConfig, IdleScanWorker, build_default_idle_scan_payload
from .scene_config import load_haiguitang_scene_config


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
        self._follow_worker: FaceFollowWorker | AttentionWorker | None = None
        self._idle_scan_worker: IdleScanWorker | None = None
        self._haiguitang_worker: HaiGuiTangWorker | None = None
        self._last_follow_payload: dict[str, Any] = build_default_attention_payload()
        self._last_idle_scan_payload: dict[str, Any] = build_default_idle_scan_payload()
        self._last_haiguitang_payload: dict[str, Any] = build_default_haiguitang_payload()

    def close(self) -> None:
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
        worker = self._idle_scan_worker
        if worker is not None:
            payload = worker.status_payload()
            self._last_idle_scan_payload = dict(payload)
            return payload
        return dict(self._last_idle_scan_payload)

    def _haiguitang_payload_locked(self) -> dict[str, Any]:
        worker = self._haiguitang_worker
        if worker is not None:
            payload = worker.status_payload()
            self._last_haiguitang_payload = dict(payload)
            return payload
        return dict(self._last_haiguitang_payload)

    def _haiguitang_config_from_last_payload_locked(self) -> HaiGuiTangConfig:
        config_payload = self._last_haiguitang_payload.get("config")
        if isinstance(config_payload, dict):
            try:
                return HaiGuiTangConfig(**config_payload)
            except TypeError:
                pass
        return HaiGuiTangConfig()

    def _deactivate_control_locked(self) -> None:
        self._control_mode = CONTROL_MODE_NONE
        if self._follow_worker is not None:
            self._last_follow_payload = self._follow_worker.status_payload()
            self._last_follow_payload["enabled"] = False
            self._last_follow_payload["running"] = False
            self._follow_worker.request_stop()
            self._follow_worker = None
        if self._idle_scan_worker is not None:
            self._last_idle_scan_payload = self._idle_scan_worker.status_payload()
            self._last_idle_scan_payload["enabled"] = False
            self._last_idle_scan_payload["running"] = False
            self._idle_scan_worker.request_stop()
            self._idle_scan_worker = None
        if self._haiguitang_worker is not None:
            self._last_haiguitang_payload = self._haiguitang_worker.status_payload()
            self._last_haiguitang_payload["enabled"] = False
            self._last_haiguitang_payload["running"] = False
            self._haiguitang_worker.request_stop()
            self._haiguitang_worker = None
        self._control_error = ""

    def _stop_for_manual_motion_locked(self) -> None:
        if self._control_mode != CONTROL_MODE_NONE:
            self._deactivate_control_locked()

    def robot_state_payload(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_robot_state_locked()
            follow_payload = self._follow_payload_locked()
            idle_scan_payload = self._idle_scan_payload_locked()
            haiguitang_payload = self._haiguitang_payload_locked()
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
            if self._control_mode in (CONTROL_MODE_FOLLOW, CONTROL_MODE_ATTENTION):
                control_error = str(follow_payload.get("last_error", "") or "").strip()
            elif self._control_mode == CONTROL_MODE_IDLE_SCAN:
                control_error = str(idle_scan_payload.get("last_error", "") or "").strip()
            elif self._control_mode == CONTROL_MODE_HAIGUITANG:
                control_error = str(haiguitang_payload.get("last_error", "") or "").strip()

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
                "idle_scan": idle_scan_payload,
                "haiguitang": haiguitang_payload,
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
        enable_idle_scan_fallback: bool,
        lost_target_hold_sec: float,
        idle_scan_speed_percent: int,
        idle_scan_pan_range_deg: float,
        idle_scan_tilt_range_deg: float,
        idle_scan_move_duration_min_sec: float,
        idle_scan_move_duration_max_sec: float,
        idle_scan_dwell_sec_min: float,
        idle_scan_dwell_sec_max: float,
    ) -> dict[str, Any]:
        with self._lock:
            robot = self._require_robot()
            self._deactivate_control_locked()
            default_latest_url = str(build_default_attention_payload()["latest_url"])
            idle_scan_move_min = float(
                max(0.2, min(idle_scan_move_duration_min_sec, idle_scan_move_duration_max_sec))
            )
            idle_scan_move_max = float(
                max(idle_scan_move_min, idle_scan_move_duration_max_sec)
            )
            idle_scan_dwell_min = float(
                max(0.0, min(idle_scan_dwell_sec_min, idle_scan_dwell_sec_max))
            )
            idle_scan_dwell_max = float(
                max(idle_scan_dwell_min, idle_scan_dwell_sec_max)
            )
            if bool(enable_idle_scan_fallback):
                worker = AttentionWorker(
                    robot=robot,
                    robot_lock=self._lock,
                    config=AttentionConfig(
                        target_kind=str(target_kind or "face"),
                        latest_url=str(latest_url or "").strip() or default_latest_url,
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
                            None
                            if pan_breakaway_step_pos is None
                            else float(max(0.0, pan_breakaway_step_pos))
                        ),
                        pan_breakaway_step_neg=(
                            float(max(0.0, pan_breakaway_step_neg))
                            if pan_breakaway_step_neg is not None
                            else float(max(0.0, pan_breakaway_step))
                        ),
                        pan_negative_scale=float(max(1.0, pan_negative_scale)),
                        tilt_breakaway_step=float(max(0.0, tilt_breakaway_step)),
                        enable_idle_scan_fallback=True,
                        lost_target_hold_sec=float(max(0.0, lost_target_hold_sec)),
                        idle_scan=IdleScanConfig(
                            pan_joint=str(pan_joint or DEFAULT_FOLLOW_PAN_JOINT),
                            tilt_joint=str(tilt_joint or DEFAULT_FOLLOW_TILT_JOINT),
                            speed_percent=int(max(1, min(100, idle_scan_speed_percent))),
                            pan_range_deg=float(np.clip(idle_scan_pan_range_deg, 1.0, 45.0)),
                            tilt_range_deg=float(np.clip(idle_scan_tilt_range_deg, 1.0, 30.0)),
                            move_duration_min_sec=idle_scan_move_min,
                            move_duration_max_sec=idle_scan_move_max,
                            dwell_sec_min=idle_scan_dwell_min,
                            dwell_sec_max=idle_scan_dwell_max,
                        ),
                    ),
                )
                self._control_mode = CONTROL_MODE_ATTENTION
            else:
                worker = FaceFollowWorker(
                    robot=robot,
                    robot_lock=self._lock,
                    config=FaceFollowConfig(
                        target_kind=str(target_kind or "face"),
                        latest_url=str(latest_url or "").strip() or default_latest_url,
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
                            None
                            if pan_breakaway_step_pos is None
                            else float(max(0.0, pan_breakaway_step_pos))
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
                self._control_mode = CONTROL_MODE_FOLLOW
            worker.start()
            self._follow_worker = worker
            self._last_follow_payload = worker.status_payload()
            self._last_follow_payload.update(
                {
                    "enabled": True,
                    "running": True,
                }
            )
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
        pan_joint: str,
        tilt_joint: str,
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
            self._deactivate_control_locked()
            move_min = float(max(0.2, min(move_duration_min_sec, move_duration_max_sec)))
            move_max = float(max(move_min, move_duration_max_sec))
            dwell_min = float(max(0.0, min(dwell_sec_min, dwell_sec_max)))
            dwell_max = float(max(dwell_min, dwell_sec_max))
            worker = IdleScanWorker(
                robot=robot,
                robot_lock=self._lock,
                config=IdleScanConfig(
                    pan_joint=str(pan_joint or DEFAULT_FOLLOW_PAN_JOINT),
                    tilt_joint=str(tilt_joint or DEFAULT_FOLLOW_TILT_JOINT),
                    speed_percent=int(max(1, min(100, speed_percent))),
                    pan_range_deg=float(np.clip(pan_range_deg, 1.0, 45.0)),
                    tilt_range_deg=float(np.clip(tilt_range_deg, 1.0, 30.0)),
                    move_duration_min_sec=move_min,
                    move_duration_max_sec=move_max,
                    dwell_sec_min=dwell_min,
                    dwell_sec_max=dwell_max,
                ),
            )
            worker.start()
            self._idle_scan_worker = worker
            self._last_idle_scan_payload = worker.status_payload()
            self._control_mode = CONTROL_MODE_IDLE_SCAN
            self._control_error = ""
            return self.idle_scan_status()

    def idle_scan_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._control_mode == CONTROL_MODE_IDLE_SCAN or self._idle_scan_worker is not None:
                self._control_mode = CONTROL_MODE_NONE
                if self._idle_scan_worker is not None:
                    self._last_idle_scan_payload = self._idle_scan_worker.status_payload()
                    self._last_idle_scan_payload["enabled"] = False
                    self._last_idle_scan_payload["running"] = False
                    self._idle_scan_worker.request_stop()
                    self._idle_scan_worker = None
            return self.idle_scan_status()

    def haiguitang_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "control_mode": str(self._control_mode),
                "haiguitang": self._haiguitang_payload_locked(),
            }

    def haiguitang_start(
        self,
        *,
        pan_joint: str,
        tilt_joint: str,
        speed_percent: int,
        nod_amplitude_deg: float,
        nod_cycles: int,
        shake_amplitude_deg: float,
        shake_cycles: int,
        beat_duration_sec: float,
        beat_pause_sec: float,
        return_duration_sec: float,
        settle_pause_sec: float,
        auto_center_after_action: bool,
        capture_anchor_on_start: bool,
    ) -> dict[str, Any]:
        with self._lock:
            robot = self._require_robot()
            self._deactivate_control_locked()
            worker = HaiGuiTangWorker(
                robot=robot,
                robot_lock=self._lock,
                config=HaiGuiTangConfig(
                    pan_joint=str(pan_joint or DEFAULT_FOLLOW_PAN_JOINT),
                    tilt_joint=str(tilt_joint or DEFAULT_FOLLOW_TILT_JOINT),
                    speed_percent=int(max(1, min(100, speed_percent))),
                    nod_amplitude_deg=float(np.clip(nod_amplitude_deg, 0.5, 25.0)),
                    nod_cycles=int(max(1, min(6, nod_cycles))),
                    shake_amplitude_deg=float(np.clip(shake_amplitude_deg, 0.5, 30.0)),
                    shake_cycles=int(max(1, min(6, shake_cycles))),
                    beat_duration_sec=float(np.clip(beat_duration_sec, 0.05, 3.0)),
                    beat_pause_sec=float(np.clip(beat_pause_sec, 0.0, 2.0)),
                    return_duration_sec=float(np.clip(return_duration_sec, 0.05, 3.0)),
                    settle_pause_sec=float(np.clip(settle_pause_sec, 0.0, 2.0)),
                    auto_center_after_action=bool(auto_center_after_action),
                    capture_anchor_on_start=bool(capture_anchor_on_start),
                ),
            )
            worker.start()
            self._haiguitang_worker = worker
            self._last_haiguitang_payload = worker.status_payload()
            self._control_mode = CONTROL_MODE_HAIGUITANG
            self._control_error = ""
            return self.haiguitang_status()

    def haiguitang_act(self, *, action: str) -> dict[str, Any]:
        with self._lock:
            self._require_robot()
            if self._control_mode != CONTROL_MODE_HAIGUITANG or self._haiguitang_worker is None:
                self._deactivate_control_locked()
                worker = HaiGuiTangWorker(
                    robot=self._require_robot(),
                    robot_lock=self._lock,
                    config=self._haiguitang_config_from_last_payload_locked(),
                )
                worker.start()
                self._haiguitang_worker = worker
                self._control_mode = CONTROL_MODE_HAIGUITANG
            self._last_haiguitang_payload = self._haiguitang_worker.enqueue_action(action)
            self._control_error = ""
            return self.haiguitang_status()

    def haiguitang_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._control_mode == CONTROL_MODE_HAIGUITANG or self._haiguitang_worker is not None:
                self._control_mode = CONTROL_MODE_NONE
                if self._haiguitang_worker is not None:
                    self._last_haiguitang_payload = self._haiguitang_worker.status_payload()
                    self._last_haiguitang_payload["enabled"] = False
                    self._last_haiguitang_payload["running"] = False
                    self._haiguitang_worker.request_stop()
                    self._haiguitang_worker = None
                robot = self._robot
                if robot is not None and getattr(robot, "connected", False):
                    robot.stop()
            return self.haiguitang_status()

    def haiguitang_scene_config(self) -> dict[str, Any]:
        return load_haiguitang_scene_config()

    def agent_status(self) -> dict[str, Any]:
        return self._agent.status_payload()

    def agent_last_turn(self) -> dict[str, Any]:
        return self._agent.last_turn_payload()

    def agent_ask(self, *, message: str) -> dict[str, Any]:
        return self._agent.ask(message)

    def agent_warmup(self, *, prompt: str = "请只回复“就绪”。") -> dict[str, Any]:
        return self._agent.warmup(prompt=prompt)

    def agent_build_stream_turn_spec(self, *, kind: str, prompt: str) -> dict[str, Any]:
        return self._agent.build_stream_turn_spec(kind=kind, prompt=prompt)

    def agent_complete_stream_turn(
        self,
        *,
        kind: str,
        prompt: str,
        reply: str,
        session_id: str,
        bridge_session_key: str,
        openclaw_elapsed_sec: float,
        bridge_timing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._agent.complete_stream_turn(
            kind=kind,
            prompt=prompt,
            reply=reply,
            session_id=session_id,
            bridge_session_key=bridge_session_key,
            openclaw_elapsed_sec=openclaw_elapsed_sec,
            bridge_timing=bridge_timing,
        )

    def agent_fail_stream_turn(
        self,
        *,
        kind: str,
        prompt: str,
        error: str,
        session_id: str = "",
        bridge_session_key: str = "",
        openclaw_elapsed_sec: float = 0.0,
        bridge_timing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._agent.fail_stream_turn(
            kind=kind,
            prompt=prompt,
            error=error,
            session_id=session_id,
            bridge_session_key=bridge_session_key,
            openclaw_elapsed_sec=openclaw_elapsed_sec,
            bridge_timing=bridge_timing,
        )

    def agent_reset_session(self) -> dict[str, Any]:
        return self._agent.reset_session()

    def agent_tts_status(self) -> dict[str, Any]:
        return self._agent.tts_status_payload()

    def agent_build_tts_stream_spec(self, *, text: str) -> dict[str, Any]:
        return self._agent.build_tts_stream_spec(text)

    def agent_set_last_turn_tts_summary(self, *, summary: dict[str, Any]) -> dict[str, Any]:
        return self._agent.set_last_turn_tts_summary(summary)
