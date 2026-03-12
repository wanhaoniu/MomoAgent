#!/usr/bin/env python3
"""SDK-style control module for the local soarmMoce follower arm."""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import draccus
import kinpy as kp
import numpy as np
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = Path(__file__).resolve().parents[1]
JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
]
ARM_JOINTS = list(JOINTS)
MULTI_TURN_JOINTS = ("shoulder_lift", "elbow_flex")
LOCKED_CARTESIAN_JOINTS = ("wrist_roll",)
MULTI_TURN_RAW_RANGE = 900000
RAW_COUNTS_PER_REV = 4096.0
HALF_RAW_COUNTS_PER_REV = RAW_COUNTS_PER_REV / 2.0
DEFAULT_TARGET_FRAME = "wrist_roll"
DEFAULT_HOME_JOINTS = {
    "shoulder_pan": -8.923076923076923,
    "shoulder_lift": -9.31868131868132,
    "elbow_flex": 8.483516483516484,
    "wrist_flex": -3.6043956043956045,
    "wrist_roll": -0.17582417582417584,
}
DEFAULT_JOINT_SCALES = {
    "shoulder_pan": 1.0,
    "shoulder_lift": 5.3,
    "elbow_flex": 5.6,
    "wrist_flex": 1.0,
    "wrist_roll": 1.0,
}
__all__ = [
    "ARM_JOINTS",
    "JOINTS",
    "HardwareError",
    "IKError",
    "SoArmMoceConfig",
    "SoArmMoceController",
    "ValidationError",
    "resolve_config",
    "to_jsonable",
]


class ValidationError(ValueError):
    pass


class HardwareError(RuntimeError):
    pass


class IKError(RuntimeError):
    pass


class IKTraceError(IKError):
    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class SoArmMoceConfig:
    port: str
    robot_id: str
    calib_dir: Path
    urdf_path: Path
    target_frame: str
    home_joints: Dict[str, float]
    joint_scales: Dict[str, float]
    arm_p_coefficient: int
    arm_d_coefficient: int
    max_ee_pos_err_m: float
    linear_step_m: float
    joint_step_deg: float
    cartesian_settle_time_s: float
    cartesian_update_hz: float
    joint_update_hz: float
    ik_target_tol_m: float
    ik_max_iters: int
    ik_damping: float
    ik_step_scale: float
    ik_joint_step_deg: float
    ik_seed_bias: float


def _env_value(*keys: str, default: str = "") -> str:
    for key in keys:
        raw = os.environ.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return default


def _load_calibration(robot_name: str, calib_dir: Path) -> dict[str, MotorCalibration]:
    fpath = calib_dir / f"{robot_name}.json"
    if not fpath.exists():
        raise FileNotFoundError(f"Calibration file not found: {fpath}")
    with open(fpath, "r", encoding="utf-8") as f, draccus.config_type("json"):
        return draccus.load(dict[str, MotorCalibration], f)


def _candidate_calibration_dirs() -> list[Path]:
    env = _env_value("SOARMMOCE_CALIB_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            SKILL_ROOT / "calibration",
            Path.home() / ".cache/huggingface/lerobot/calibration/robots/so101_follower",
            REPO_ROOT / "Software/Slave/calibration/robots/so101_follower",
            REPO_ROOT / "Software/Master/calibration/robots/so101_follower",
            Path.cwd() / "Software/Slave/calibration/robots/so101_follower",
            Path.home() / "Code/SO-ARM-Moce/Software/Slave/calibration/robots/so101_follower",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _candidate_calibration_robot_ids() -> list[str]:
    env_robot_id = _env_value("SOARMMOCE_ROBOT_ID")
    if env_robot_id:
        return [env_robot_id]
    candidates = ["soarmmoce", "follower_moce"]
    unique: list[str] = []
    seen: set[str] = set()
    for robot_id in candidates:
        key = str(robot_id).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _resolve_calibration_target() -> tuple[str, Path]:
    searched: list[str] = []
    for candidate_dir in _candidate_calibration_dirs():
        for robot_id in _candidate_calibration_robot_ids():
            fpath = candidate_dir / f"{robot_id}.json"
            searched.append(str(fpath))
            if fpath.exists():
                return robot_id, candidate_dir.resolve()
    raise FileNotFoundError(
        "Could not find calibration file. Searched: "
        + str(searched)
        + ". Set SOARMMOCE_CALIB_DIR and/or SOARMMOCE_ROBOT_ID explicitly."
    )


def _candidate_urdf_paths() -> list[Path]:
    env = _env_value("SOARMMOCE_URDF_PATH")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            REPO_ROOT / "sdk/src/soarmmoce_sdk/resources/urdf/soarmoce_urdf.urdf",
            Path.cwd() / "sdk/src/soarmmoce_sdk/resources/urdf/soarmoce_urdf.urdf",
            Path.home() / "Code/SO-ARM-Moce/sdk/src/soarmmoce_sdk/resources/urdf/soarmoce_urdf.urdf",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _resolve_urdf_path() -> Path:
    for candidate in _candidate_urdf_paths():
        if candidate.exists():
            return candidate.resolve()
    return _candidate_urdf_paths()[0]


def _resolve_home_joints() -> Dict[str, float]:
    raw = _env_value("SOARMMOCE_HOME_JOINTS_JSON")
    if not raw:
        return {name: float(value) for name, value in DEFAULT_HOME_JOINTS.items()}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in HOME_JOINTS_JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("HOME_JOINTS_JSON must be a JSON object")
    home_joints = {name: float(value) for name, value in DEFAULT_HOME_JOINTS.items()}
    for joint_name, joint_value in payload.items():
        joint = str(joint_name).strip()
        if joint not in JOINTS:
            raise ValidationError(f"Unknown joint in HOME_JOINTS_JSON: {joint}")
        if not isinstance(joint_value, (int, float)):
            raise ValidationError(f"Home joint value for {joint} must be numeric")
        home_joints[joint] = float(joint_value)
    return home_joints


def _resolve_joint_scales() -> Dict[str, float]:
    raw = _env_value("SOARMMOCE_JOINT_SCALE_JSON")
    scales = {name: float(value) for name, value in DEFAULT_JOINT_SCALES.items()}
    if not raw:
        return scales
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in JOINT_SCALE_JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("JOINT_SCALE_JSON must be a JSON object")
    for joint_name, joint_value in payload.items():
        joint = str(joint_name).strip()
        if joint not in JOINTS:
            raise ValidationError(f"Unknown joint in JOINT_SCALE_JSON: {joint}")
        if not isinstance(joint_value, (int, float)):
            raise ValidationError(f"Joint scale for {joint} must be numeric")
        scale = float(joint_value)
        if abs(scale) < 1e-9:
            raise ValidationError(f"Joint scale for {joint} must be non-zero")
        scales[joint] = scale
    return scales


def resolve_config() -> SoArmMoceConfig:
    port = _env_value("SOARMMOCE_PORT", default="/dev/ttyACM0")
    urdf_path = _resolve_urdf_path()
    target_frame = _env_value("SOARMMOCE_TARGET_FRAME", default=DEFAULT_TARGET_FRAME)
    robot_id, chosen_dir = _resolve_calibration_target()

    return SoArmMoceConfig(
        port=port,
        robot_id=robot_id,
        calib_dir=chosen_dir,
        urdf_path=urdf_path,
        target_frame=target_frame,
        home_joints=_resolve_home_joints(),
        joint_scales=_resolve_joint_scales(),
        arm_p_coefficient=int(_env_value("SOARMMOCE_ARM_P_COEFFICIENT", default="16")),
        arm_d_coefficient=int(_env_value("SOARMMOCE_ARM_D_COEFFICIENT", default="8")),
        max_ee_pos_err_m=float(_env_value("SOARMMOCE_MAX_EE_POS_ERR_M", default="0.01")),
        linear_step_m=float(_env_value("SOARMMOCE_LINEAR_STEP_M", default="0.01")),
        joint_step_deg=float(_env_value("SOARMMOCE_JOINT_STEP_DEG", default="5.0")),
        cartesian_settle_time_s=float(_env_value("SOARMMOCE_CARTESIAN_SETTLE_TIME_S", default="0.15")),
        cartesian_update_hz=float(_env_value("SOARMMOCE_CARTESIAN_UPDATE_HZ", default="20.0")),
        joint_update_hz=float(_env_value("SOARMMOCE_JOINT_UPDATE_HZ", default="25.0")),
        ik_target_tol_m=float(_env_value("SOARMMOCE_IK_TARGET_TOL_M", default="0.001")),
        ik_max_iters=int(_env_value("SOARMMOCE_IK_MAX_ITERS", default="200")),
        ik_damping=float(_env_value("SOARMMOCE_IK_DAMPING", default="0.05")),
        ik_step_scale=float(_env_value("SOARMMOCE_IK_STEP_SCALE", default="0.8")),
        ik_joint_step_deg=float(_env_value("SOARMMOCE_IK_JOINT_STEP_DEG", default="8.0")),
        ik_seed_bias=float(_env_value("SOARMMOCE_IK_SEED_BIAS", default="0.02")),
    )


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return to_jsonable(value.tolist())
    if hasattr(value, "__dict__"):
        return {k: to_jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _make_passthrough_unnormalize(original_method, passthrough_ids: set[int]):
    def hybrid_unnormalize(self, ids_values: dict[int, float]) -> dict[int, int]:
        result = {}
        for motor_id, value in ids_values.items():
            if motor_id in passthrough_ids:
                result[motor_id] = int(value)
            else:
                result.update(original_method({motor_id: value}))
        return result

    return hybrid_unnormalize


class SoArmMoceController:
    def __init__(self, config: Optional[SoArmMoceConfig] = None):
        self.config = config or resolve_config()
        self._lock = threading.Lock()
        self._bus: Optional[FeetechMotorsBus] = None
        self._kin_chain = None
        self._multi_turn_state: Dict[str, Dict[str, float]] = {}

    def __enter__(self) -> "SoArmMoceController":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._bus is None:
            return
        disconnect = getattr(self._bus, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass
        self._bus = None
        self._multi_turn_state = {}

    def meta(self) -> Dict[str, Any]:
        return {
            "connected": True,
            "robot_type": "soarmmoce",
            "robot_id": self.config.robot_id,
            "port": self.config.port,
            "joint_names": list(JOINTS),
            "multi_turn_joints": list(MULTI_TURN_JOINTS),
            "joint_scales": dict(self.config.joint_scales),
            "ik_mode": "5dof_position_only",
            "cartesian_locked_joints": list(LOCKED_CARTESIAN_JOINTS),
            "gripper_available": False,
        }

    def get_state(self) -> Dict[str, Any]:
        bus = self._ensure_bus()
        joints = self._read_joint_state_from_bus(bus)
        tf = self._forward_kinematics_from_arm_deg(np.array([joints[name] for name in ARM_JOINTS], dtype=float))
        pose = self._transform_to_pose_dict(tf)
        pose.pop("rot_matrix", None)
        return {
            "joint_state": joints,
            "tcp_pose": pose,
            "gripper": {
                "available": False,
                "installed": False,
                "message": "gripper servo is not installed on this soarmMoce arm",
            },
            "timestamp": time.time(),
        }

    def read(self) -> Dict[str, Any]:
        return {"meta": self.meta(), "state": self.get_state()}

    def diagnose_ik(
        self,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        frame: str = "base",
        repeats: int = 12,
        seed_jitter_deg: float = 0.1,
        random_seed: int = 0,
    ) -> Dict[str, Any]:
        if abs(dx) < 1e-12 and abs(dy) < 1e-12 and abs(dz) < 1e-12:
            raise ValidationError("At least one of dx/dy/dz must be non-zero")
        if frame not in {"base", "tool"}:
            raise ValidationError("frame must be 'base' or 'tool'")

        state = self.get_state()
        q_seed_deg = self._state_to_arm_q_deg(state)
        current_tf = self._forward_kinematics_from_arm_deg(q_seed_deg)
        delta = np.array([float(dx), float(dy), float(dz)], dtype=float)
        if frame == "tool":
            target_pos = np.asarray(current_tf.pos, dtype=float) + current_tf.rot_mat @ delta
        else:
            target_pos = np.asarray(current_tf.pos, dtype=float) + delta

        locked_joint_targets_deg = {
            joint_name: float(state["joint_state"][joint_name]) for joint_name in LOCKED_CARTESIAN_JOINTS
        }
        locked_indices = [ARM_JOINTS.index(name) for name in LOCKED_CARTESIAN_JOINTS if name in ARM_JOINTS]
        active_indices = [idx for idx in range(len(ARM_JOINTS)) if idx not in locked_indices]

        jacobian_full = np.asarray(self._ensure_kin_chain().jacobian(np.deg2rad(q_seed_deg)), dtype=float)[:3, :]
        jacobian_pos = jacobian_full[:, active_indices]
        singular_values = np.linalg.svd(jacobian_pos, compute_uv=False)
        min_sv = float(np.min(singular_values)) if singular_values.size else 0.0
        max_sv = float(np.max(singular_values)) if singular_values.size else 0.0
        condition_number = float(max_sv / min_sv) if min_sv > 1e-12 else float("inf")

        rng = np.random.default_rng(int(random_seed))
        solve_runs: list[Dict[str, Any]] = []
        run_joint_targets: list[np.ndarray] = []
        run_tcp_deltas: list[np.ndarray] = []
        requested_repeats = max(1, int(repeats))
        jitter_deg = max(0.0, float(seed_jitter_deg))

        for run_index in range(requested_repeats):
            q_trial_deg = q_seed_deg.copy()
            if run_index > 0 and jitter_deg > 0.0:
                jitter = rng.uniform(-jitter_deg, jitter_deg, size=len(active_indices))
                q_trial_deg[active_indices] = q_trial_deg[active_indices] + jitter
            ik = self._solve_ik_to_position(
                target_pos,
                q_trial_deg,
                locked_joint_targets_deg=locked_joint_targets_deg,
            )
            q_target_deg = np.asarray(ik["q_target_deg"], dtype=float)
            achieved_tf = self._forward_kinematics_from_arm_deg(q_target_deg)
            achieved_delta = np.asarray(achieved_tf.pos, dtype=float) - np.asarray(current_tf.pos, dtype=float)
            q_delta_deg = q_target_deg - q_seed_deg
            run_joint_targets.append(q_target_deg)
            run_tcp_deltas.append(achieved_delta)
            solve_runs.append(
                {
                    "run_index": run_index,
                    "seed_deg": {name: float(q_trial_deg[idx]) for idx, name in enumerate(ARM_JOINTS)},
                    "q_target_deg": {name: float(q_target_deg[idx]) for idx, name in enumerate(ARM_JOINTS)},
                    "q_delta_deg": {name: float(q_delta_deg[idx]) for idx, name in enumerate(ARM_JOINTS)},
                    "achieved_tcp_delta": self._xyz_dict(achieved_delta),
                    "pos_err_m": float(ik["pos_err_m"]),
                    "iterations": int(ik["iterations"]),
                }
            )

        joint_targets_arr = np.vstack(run_joint_targets)
        tcp_deltas_arr = np.vstack(run_tcp_deltas)
        return {
            "note": "read-only IK diagnosis, does not move hardware",
            "request": {"dx": float(dx), "dy": float(dy), "dz": float(dz), "frame": frame},
            "state": state,
            "target_tcp": self._xyz_dict(target_pos),
            "jacobian": {
                "locked_joints": list(LOCKED_CARTESIAN_JOINTS),
                "active_joints": [ARM_JOINTS[idx] for idx in active_indices],
                "singular_values": [float(value) for value in singular_values],
                "condition_number": condition_number,
            },
            "solver": {
                "repeats": requested_repeats,
                "seed_jitter_deg": jitter_deg,
                "random_seed": int(random_seed),
            },
            "summary": {
                "target_joint_span_deg": {
                    name: float(np.max(joint_targets_arr[:, idx]) - np.min(joint_targets_arr[:, idx]))
                    for idx, name in enumerate(ARM_JOINTS)
                },
                "achieved_tcp_span_m": {
                    "x": float(np.max(tcp_deltas_arr[:, 0]) - np.min(tcp_deltas_arr[:, 0])),
                    "y": float(np.max(tcp_deltas_arr[:, 1]) - np.min(tcp_deltas_arr[:, 1])),
                    "z": float(np.max(tcp_deltas_arr[:, 2]) - np.min(tcp_deltas_arr[:, 2])),
                },
                "best_pos_err_m": float(min(run["pos_err_m"] for run in solve_runs)),
                "worst_pos_err_m": float(max(run["pos_err_m"] for run in solve_runs)),
            },
            "runs": solve_runs,
        }

    def move_to(
        self,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
        trace: bool = False,
    ) -> Dict[str, Any]:
        if x is None and y is None and z is None:
            raise ValidationError("At least one of x/y/z is required")
        before = self.get_state()
        q_seed_deg = self._state_to_arm_q_deg(before)
        current_tf = self._forward_kinematics_from_arm_deg(q_seed_deg)
        target_pos = np.array(
            [
                float(current_tf.pos[0]) if x is None else float(x),
                float(current_tf.pos[1]) if y is None else float(y),
                float(current_tf.pos[2]) if z is None else float(z),
            ],
            dtype=float,
        )
        trace_steps: Optional[list[Dict[str, Any]]] = [] if trace else None
        state = self._move_tcp_smooth(
            start_state=before,
            target_pos=target_pos,
            duration=duration,
            wait=wait,
            timeout=timeout,
            trace_steps=trace_steps,
        )
        result = {
            "action": "move_to",
            "target_tcp": self._xyz_dict(target_pos),
            "tcp_delta": self._tcp_delta(before, state),
            "state": state,
        }
        if trace_steps is not None:
            result["trace"] = self._finalize_trace(trace_steps)
        return result

    def move_delta(
        self,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        frame: str = "base",
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
        trace: bool = False,
    ) -> Dict[str, Any]:
        if abs(dx) < 1e-12 and abs(dy) < 1e-12 and abs(dz) < 1e-12:
            raise ValidationError("At least one of dx/dy/dz must be non-zero")
        if frame not in {"base", "tool"}:
            raise ValidationError("frame must be 'base' or 'tool'")
        delta = np.array([float(dx), float(dy), float(dz)], dtype=float)

        before = self.get_state()
        q_seed_deg = self._state_to_arm_q_deg(before)
        current_tf = self._forward_kinematics_from_arm_deg(q_seed_deg)
        if frame == "tool":
            target_pos = np.asarray(current_tf.pos, dtype=float) + current_tf.rot_mat @ delta
        else:
            target_pos = np.asarray(current_tf.pos, dtype=float) + delta
        trace_steps: Optional[list[Dict[str, Any]]] = [] if trace else None
        state = self._move_tcp_smooth(
            start_state=before,
            target_pos=target_pos,
            duration=duration,
            wait=wait,
            timeout=timeout,
            trace_steps=trace_steps,
        )
        result = {
            "action": "move_delta",
            "requested_delta": {"dx": float(dx), "dy": float(dy), "dz": float(dz), "frame": frame},
            "tcp_delta": self._tcp_delta(before, state),
            "state": state,
        }
        if trace_steps is not None:
            result["trace"] = self._finalize_trace(trace_steps)
        return result

    def move_joint(
        self,
        *,
        joint: str,
        delta_deg: Optional[float] = None,
        target_deg: Optional[float] = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
        trace: bool = False,
    ) -> Dict[str, Any]:
        joint_name = self._validate_joint_name(joint)
        if (delta_deg is None) == (target_deg is None):
            raise ValidationError("Exactly one of delta_deg or target_deg must be provided")
        before = self.get_state()
        current = float(before["joint_state"][joint_name])
        target = float(current + float(delta_deg)) if delta_deg is not None else float(target_deg)
        target_cmd = {name: float(before["joint_state"][name]) for name in JOINTS}
        target_cmd[joint_name] = target
        trace_steps: Optional[list[Dict[str, Any]]] = [] if trace else None
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd=target_cmd,
            duration=duration,
            wait=wait,
            timeout=timeout,
            trace_steps=trace_steps,
        )
        result = {
            "action": "move_joint",
            "joint": joint_name,
            "delta_deg": float(target - current),
            "target_deg": target,
            "state": state,
        }
        if trace_steps is not None:
            result["trace"] = self._finalize_trace(trace_steps)
        return result

    def move_joints(
        self,
        *,
        targets_deg: Dict[str, float],
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
        trace: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(targets_deg, dict) or not targets_deg:
            raise ValidationError("targets_deg must be a non-empty object")
        before = self.get_state()
        cmd: Dict[str, float] = {name: float(before["joint_state"][name]) for name in JOINTS}
        for raw_joint, raw_value in targets_deg.items():
            joint = self._validate_joint_name(str(raw_joint))
            if not isinstance(raw_value, (int, float)):
                raise ValidationError(f"targets_deg.{joint} must be a number")
            cmd[joint] = float(raw_value)
        trace_steps: Optional[list[Dict[str, Any]]] = [] if trace else None
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd=cmd,
            duration=duration,
            wait=wait,
            timeout=timeout,
            trace_steps=trace_steps,
        )
        result = {
            "action": "move_joints",
            "targets_deg": cmd,
            "state": state,
        }
        if trace_steps is not None:
            result["trace"] = self._finalize_trace(trace_steps)
        return result

    def home(
        self,
        *,
        duration: float = 1.5,
        wait: bool = True,
        timeout: Optional[float] = None,
        trace: bool = False,
    ) -> Dict[str, Any]:
        before = self.get_state()
        trace_steps: Optional[list[Dict[str, Any]]] = [] if trace else None
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd=dict(self.config.home_joints),
            duration=duration,
            wait=wait,
            timeout=timeout,
            trace_steps=trace_steps,
        )
        result = {"action": "home", "target_joints": dict(self.config.home_joints), "state": state}
        if trace_steps is not None:
            result["trace"] = self._finalize_trace(trace_steps)
        return result

    def set_gripper(
        self,
        *,
        open_ratio: float,
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        raise HardwareError("Gripper servo is not installed on this soarmMoce arm")

    def open_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        raise HardwareError("Gripper servo is not installed on this soarmMoce arm")

    def close_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        raise HardwareError("Gripper servo is not installed on this soarmMoce arm")

    def stop(self) -> Dict[str, Any]:
        state = self._hold_current_pose()
        return {"action": "stop", "held": True, "state": state}

    def _ensure_bus(self) -> FeetechMotorsBus:
        with self._lock:
            if self._bus is not None:
                return self._bus

            calib = _load_calibration(self.config.robot_id, self.config.calib_dir)
            for name in MULTI_TURN_JOINTS:
                if name in calib:
                    calib[name].range_min = -MULTI_TURN_RAW_RANGE
                    calib[name].range_max = MULTI_TURN_RAW_RANGE

            bus = FeetechMotorsBus(
                port=self.config.port,
                motors={
                    "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
                    "shoulder_lift": Motor(2, "sts3215", MotorNormMode.DEGREES),
                    "elbow_flex": Motor(3, "sts3215", MotorNormMode.DEGREES),
                    "wrist_flex": Motor(4, "sts3215", MotorNormMode.DEGREES),
                    "wrist_roll": Motor(5, "sts3215", MotorNormMode.DEGREES),
                },
                calibration=calib,
            )

            passthrough_ids = {bus.motors[name].id for name in MULTI_TURN_JOINTS}
            bus._unnormalize = types.MethodType(_make_passthrough_unnormalize(bus._unnormalize, passthrough_ids), bus)

            bus.connect()
            with bus.torque_disabled():
                bus.configure_motors()
                for name in JOINTS:
                    if name in MULTI_TURN_JOINTS:
                        bus.write("Lock", name, 0)
                        time.sleep(0.02)
                        bus.write("Min_Position_Limit", name, 0)
                        bus.write("Max_Position_Limit", name, 0)
                        bus.write("Operating_Mode", name, 3)
                        time.sleep(0.02)
                        bus.write("Lock", name, 1)
                    else:
                        bus.write("Operating_Mode", name, OperatingMode.POSITION.value)
                        bus.write("P_Coefficient", name, self.config.arm_p_coefficient)
                        bus.write("I_Coefficient", name, 0)
                        bus.write("D_Coefficient", name, self.config.arm_d_coefficient)

            bus.enable_torque()
            current_joint_deg = self._read_joint_state_from_bus(bus)
            hold_cmd = self._build_bus_command(current_joint_deg, current_joint_deg=current_joint_deg)
            if hold_cmd:
                bus.sync_write("Goal_Position", hold_cmd)
            self._bus = bus
            return bus

    def _ensure_kin_chain(self):
        if self._kin_chain is not None:
            return self._kin_chain
        if not self.config.urdf_path.exists():
            raise FileNotFoundError(f"URDF not found: {self.config.urdf_path}")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self._kin_chain = kp.build_serial_chain_from_urdf(
                self.config.urdf_path.read_text().encode("utf-8"),
                end_link_name=self.config.target_frame,
            )
        return self._kin_chain

    @staticmethod
    def _wait(duration: float, wait: bool, timeout: Optional[float]) -> None:
        if not wait:
            return
        delay = max(0.0, float(duration))
        if timeout is not None and timeout < delay:
            time.sleep(max(0.0, float(timeout)))
            raise TimeoutError(f"timeout exceeded before nominal duration {delay:.3f}s")
        time.sleep(delay)

    @staticmethod
    def _validate_joint_name(name: str) -> str:
        joint = str(name or "").strip()
        if joint not in JOINTS:
            raise ValidationError(f"Unknown joint: {joint}")
        return joint

    @staticmethod
    def _state_to_arm_q_deg(state: Dict[str, Any]) -> np.ndarray:
        return np.array([float(state["joint_state"][name]) for name in ARM_JOINTS], dtype=float)

    @staticmethod
    def _transform_to_pose_dict(tf: kp.Transform) -> Dict[str, Any]:
        quat_wxyz = np.asarray(tf.rot, dtype=float)
        return {
            "xyz": np.asarray(tf.pos, dtype=float),
            "rpy": np.asarray(tf.rot_euler, dtype=float),
            "quat_wxyz": quat_wxyz,
            "rot_matrix": np.asarray(tf.rot_mat, dtype=float),
        }

    def _forward_kinematics_from_arm_deg(self, q_arm_deg: np.ndarray) -> kp.Transform:
        chain = self._ensure_kin_chain()
        q_rad = np.deg2rad(np.asarray(q_arm_deg, dtype=float).reshape(-1))
        return chain.forward_kinematics(q_rad)

    def _solve_ik_to_position(
        self,
        target_pos: np.ndarray,
        q_seed_deg: np.ndarray,
        locked_joint_targets_deg: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        chain = self._ensure_kin_chain()
        q_seed_deg = np.asarray(q_seed_deg, dtype=float).reshape(-1)
        if q_seed_deg.shape[0] != len(ARM_JOINTS):
            raise IKError(f"Expected {len(ARM_JOINTS)} seed joints, got {q_seed_deg.shape[0]}")

        q_seed_rad = np.deg2rad(q_seed_deg)
        target_pos = np.asarray(target_pos, dtype=float).reshape(3)
        q_current = q_seed_rad.copy()
        locked_joint_targets_deg = dict(locked_joint_targets_deg or {})
        locked_indices = []
        for joint_name, joint_target_deg in locked_joint_targets_deg.items():
            if joint_name not in ARM_JOINTS:
                raise IKError(f"Cannot lock non-arm joint in IK: {joint_name}")
            idx = ARM_JOINTS.index(joint_name)
            q_current[idx] = np.deg2rad(float(joint_target_deg))
            locked_indices.append(idx)
        active_indices = [idx for idx in range(len(ARM_JOINTS)) if idx not in locked_indices]
        if not active_indices:
            raise IKError("No active joints left for IK solve")

        damping = max(1e-6, float(self.config.ik_damping))
        step_scale = max(1e-3, float(self.config.ik_step_scale))
        max_step_rad = np.deg2rad(max(0.1, float(self.config.ik_joint_step_deg)))
        seed_bias = max(0.0, float(self.config.ik_seed_bias))
        solve_tol_m = max(1e-5, float(self.config.ik_target_tol_m))
        failure_tol_m = max(solve_tol_m, float(self.config.max_ee_pos_err_m))
        identity_pos = np.eye(3, dtype=float)
        identity_joint = np.eye(len(active_indices), dtype=float)

        best_q = q_current.copy()
        best_err = float("inf")

        for iteration in range(1, max(1, int(self.config.ik_max_iters)) + 1):
            current_tf = chain.forward_kinematics(q_current)
            current_pos = np.asarray(current_tf.pos, dtype=float)
            pos_err_vec = target_pos - current_pos
            pos_err = float(np.linalg.norm(pos_err_vec))
            if pos_err < best_err:
                best_err = pos_err
                best_q = q_current.copy()
            if pos_err <= solve_tol_m:
                return {
                    "q_target_deg": np.rad2deg(q_current),
                    "pos_err_m": pos_err,
                    "iterations": iteration,
                }

            jacobian_full = np.asarray(chain.jacobian(q_current), dtype=float)[:3, :]
            if jacobian_full.shape != (3, len(ARM_JOINTS)):
                raise IKError(f"Unexpected Jacobian shape: {jacobian_full.shape}")
            jacobian_pos = jacobian_full[:, active_indices]
            if jacobian_pos.shape != (3, len(active_indices)):
                raise IKError(f"Unexpected Jacobian shape: {jacobian_pos.shape}")

            jj_t = jacobian_pos @ jacobian_pos.T
            damped_system = jj_t + (damping**2) * identity_pos
            try:
                primary_step = jacobian_pos.T @ np.linalg.solve(damped_system, pos_err_vec)
                if seed_bias > 0.0:
                    null_projector = identity_joint - (jacobian_pos.T @ np.linalg.solve(damped_system, jacobian_pos))
                    secondary_step = null_projector @ (q_seed_rad[active_indices] - q_current[active_indices])
                else:
                    secondary_step = np.zeros(len(active_indices), dtype=float)
            except np.linalg.LinAlgError as exc:
                raise IKError(f"IK linear solve failed: {exc}") from exc

            joint_step_active = (primary_step + seed_bias * secondary_step) * step_scale
            joint_step_active = np.clip(joint_step_active, -max_step_rad, max_step_rad)
            if not np.all(np.isfinite(joint_step_active)):
                raise IKError("IK solver produced non-finite joint update")
            if np.linalg.norm(joint_step_active) < 1e-9:
                break
            q_current = q_current.copy()
            q_current[active_indices] = q_current[active_indices] + joint_step_active
            for idx in locked_indices:
                q_current[idx] = np.deg2rad(float(locked_joint_targets_deg[ARM_JOINTS[idx]]))

        if best_err <= failure_tol_m:
            return {
                "q_target_deg": np.rad2deg(best_q),
                "pos_err_m": best_err,
                "iterations": int(self.config.ik_max_iters),
            }
        raise IKError(
            f"5DOF IK position error too large: {best_err:.6f} m "
            f"(limit {failure_tol_m:.6f} m)"
        )

    def _joint_scale(self, joint_name: str) -> float:
        return float(self.config.joint_scales.get(joint_name, 1.0))

    def _motor_to_joint_deg(self, joint_name: str, motor_deg: float) -> float:
        return float(motor_deg) / self._joint_scale(joint_name)

    def _joint_to_motor_deg(self, joint_name: str, joint_deg: float) -> float:
        return float(joint_deg) * self._joint_scale(joint_name)

    def _unwrap_multi_turn_raw(self, joint_name: str, raw_value: float) -> float:
        raw_scalar = float(raw_value)
        raw_mod = float(raw_scalar % RAW_COUNTS_PER_REV)
        state = self._multi_turn_state.get(joint_name)
        if state is None:
            continuous_raw = raw_mod
        else:
            last_raw_mod = float(state["last_raw_mod"])
            last_continuous_raw = float(state["continuous_raw"])
            delta = raw_mod - last_raw_mod
            if delta > HALF_RAW_COUNTS_PER_REV:
                delta -= RAW_COUNTS_PER_REV
            elif delta < -HALF_RAW_COUNTS_PER_REV:
                delta += RAW_COUNTS_PER_REV
            continuous_raw = last_continuous_raw + delta
        self._multi_turn_state[joint_name] = {
            "last_raw_mod": raw_mod,
            "continuous_raw": continuous_raw,
        }
        return continuous_raw

    def _multi_turn_raw_to_joint_deg(self, joint_name: str, raw_value: float) -> float:
        continuous_raw = self._unwrap_multi_turn_raw(joint_name, raw_value)
        motor_deg = continuous_raw * 360.0 / RAW_COUNTS_PER_REV
        return self._motor_to_joint_deg(joint_name, motor_deg)

    def _snapshot_multi_turn_state(self) -> Dict[str, Dict[str, float]]:
        return {
            name: {
                "last_raw_mod": float(state["last_raw_mod"]),
                "continuous_raw": float(state["continuous_raw"]),
                "motor_deg": float(state["continuous_raw"] * 360.0 / RAW_COUNTS_PER_REV),
                "joint_deg": float(
                    self._motor_to_joint_deg(name, state["continuous_raw"] * 360.0 / RAW_COUNTS_PER_REV)
                ),
            }
            for name, state in self._multi_turn_state.items()
        }

    def _read_joint_state_from_bus(self, bus: FeetechMotorsBus) -> Dict[str, float]:
        raw_motor = bus.sync_read("Present_Position", normalize=False)
        current_motor = bus.sync_read("Present_Position")
        joints: Dict[str, float] = {}
        for name in JOINTS:
            if name in MULTI_TURN_JOINTS:
                joints[name] = self._multi_turn_raw_to_joint_deg(name, float(raw_motor.get(name, 0.0)))
            else:
                joints[name] = self._motor_to_joint_deg(name, float(current_motor.get(name, 0.0)))
        return joints

    def _get_current_joint_state(self) -> Dict[str, float]:
        return self._read_joint_state_from_bus(self._ensure_bus())

    @staticmethod
    def _read_raw_present_position(bus: FeetechMotorsBus) -> Dict[str, int]:
        raw_motor = bus.sync_read("Present_Position", normalize=False)
        return {name: int(raw_motor.get(name, 0)) for name in JOINTS}

    @staticmethod
    def _joint_error_deg(target_joint_deg: Dict[str, float], actual_joint_deg: Dict[str, float]) -> Dict[str, float]:
        return {
            name: float(actual_joint_deg[name] - float(target_joint_deg[name]))
            for name in target_joint_deg
            if name in actual_joint_deg
        }

    @staticmethod
    def _raw_delta(before_raw: Dict[str, int], after_raw: Dict[str, int]) -> Dict[str, int]:
        keys = list(dict.fromkeys(list(before_raw.keys()) + list(after_raw.keys())))
        return {name: int(after_raw.get(name, 0) - before_raw.get(name, 0)) for name in keys}

    def _build_bus_command(
        self,
        target_joint_deg: Dict[str, float],
        current_joint_deg: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        current_joint_deg = current_joint_deg or self._get_current_joint_state()
        cmd: Dict[str, float] = {}
        for name, target in target_joint_deg.items():
            if name in MULTI_TURN_JOINTS:
                current = float(current_joint_deg[name])
                delta_motor_deg = self._joint_to_motor_deg(name, float(target) - current)
                raw_step = int(delta_motor_deg * RAW_COUNTS_PER_REV / 360.0)
                if raw_step != 0:
                    cmd[name] = float(raw_step)
            else:
                cmd[name] = self._joint_to_motor_deg(name, float(target))
        return cmd

    def _move_goal(
        self,
        cmd: Dict[str, float],
        *,
        duration: float,
        wait: bool,
        timeout: Optional[float],
        command_reference_joint_deg: Optional[Dict[str, float]] = None,
        trace_entry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        bus = self._ensure_bus()
        current_joint_deg = self._read_joint_state_from_bus(bus)
        command_reference_joint_deg = dict(command_reference_joint_deg or current_joint_deg)
        raw_before = self._read_raw_present_position(bus) if trace_entry is not None else None
        bus_cmd = self._build_bus_command(cmd, current_joint_deg=command_reference_joint_deg)
        if bus_cmd:
            bus.sync_write("Goal_Position", bus_cmd)
        self._wait(duration, wait, timeout)
        state = self.get_state()
        if trace_entry is not None:
            raw_after = self._read_raw_present_position(bus)
            trace_entry.update(
                {
                    "before_joint_deg": {name: float(current_joint_deg[name]) for name in JOINTS},
                    "command_reference_joint_deg": {
                        name: float(command_reference_joint_deg[name]) for name in JOINTS if name in command_reference_joint_deg
                    },
                    "after_joint_deg": {name: float(state["joint_state"][name]) for name in JOINTS},
                    "target_joint_deg": {name: float(cmd[name]) for name in cmd},
                    "joint_error_deg": self._joint_error_deg(cmd, state["joint_state"]),
                    "bus_cmd": {name: float(value) for name, value in bus_cmd.items()},
                    "multi_turn_raw_step_cmd": {
                        name: int(round(bus_cmd[name])) for name in MULTI_TURN_JOINTS if name in bus_cmd
                    },
                    "raw_present_position_before": raw_before,
                    "raw_present_position_after": raw_after,
                    "raw_present_position_delta": self._raw_delta(raw_before or {}, raw_after),
                    "multi_turn_state": self._snapshot_multi_turn_state(),
                    "wait_s": float(duration),
                }
            )
        return state

    def _hold_current_pose(self) -> Dict[str, Any]:
        state = self.get_state()
        hold_target = {name: float(state["joint_state"][name]) for name in JOINTS if name not in MULTI_TURN_JOINTS}
        hold_cmd = self._build_bus_command(hold_target, current_joint_deg=state["joint_state"])
        if hold_cmd:
            self._ensure_bus().sync_write("Goal_Position", hold_cmd)
        return state

    def _move_tcp_smooth(
        self,
        *,
        start_state: Dict[str, Any],
        target_pos: np.ndarray,
        duration: float,
        wait: bool,
        timeout: Optional[float],
        trace_steps: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        start_xyz = np.asarray(start_state["tcp_pose"]["xyz"], dtype=float)
        q_seed_deg = self._state_to_arm_q_deg(start_state)
        command_reference_joint_deg = {name: float(start_state["joint_state"][name]) for name in JOINTS}
        locked_joint_targets_deg = {
            joint_name: float(start_state["joint_state"][joint_name]) for joint_name in LOCKED_CARTESIAN_JOINTS
        }
        state = start_state
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        try:
            if not wait:
                ik = self._solve_ik_to_position(target_pos, q_seed_deg, locked_joint_targets_deg=locked_joint_targets_deg)
                cmd = {name: float(ik["q_target_deg"][idx]) for idx, name in enumerate(ARM_JOINTS)}
                trace_entry = None
                if trace_steps is not None:
                    trace_entry = {
                        "mode": "cartesian",
                        "step_index": 1,
                        "steps_total": 1,
                        "alpha": 1.0,
                        "waypoint_tcp_target": self._xyz_dict(target_pos),
                        "ik_target_joint_deg": cmd,
                        "ik_pos_err_m": float(ik["pos_err_m"]),
                        "ik_iterations": int(ik["iterations"]),
                    }
                state = self._move_goal(
                    cmd,
                    duration=duration,
                    wait=False,
                    timeout=timeout,
                    command_reference_joint_deg=command_reference_joint_deg,
                    trace_entry=trace_entry,
                )
                if trace_entry is not None:
                    trace_steps.append(trace_entry)
                command_reference_joint_deg.update({name: float(value) for name, value in cmd.items()})
                return state

            step_m = max(1e-4, float(self.config.linear_step_m))
            distance = float(np.linalg.norm(target_pos - start_xyz))
            hz_steps = int(np.ceil(max(0.0, float(duration)) * max(1.0, float(self.config.cartesian_update_hz))))
            steps = max(1, int(np.ceil(distance / step_m)), hz_steps)
            step_duration = max(0.0, float(duration)) / steps if steps else 0.0
            for step_index in range(1, steps + 1):
                alpha = self._smooth_fraction(float(step_index) / float(steps))
                waypoint_pos = start_xyz + (target_pos - start_xyz) * alpha
                ik = self._solve_ik_to_position(
                    waypoint_pos,
                    q_seed_deg,
                    locked_joint_targets_deg=locked_joint_targets_deg,
                )
                cmd = {name: float(ik["q_target_deg"][idx]) for idx, name in enumerate(ARM_JOINTS)}
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                trace_entry = None
                if trace_steps is not None:
                    trace_entry = {
                        "mode": "cartesian",
                        "step_index": int(step_index),
                        "steps_total": int(steps),
                        "alpha": float(alpha),
                        "waypoint_tcp_target": self._xyz_dict(waypoint_pos),
                        "ik_target_joint_deg": cmd,
                        "ik_pos_err_m": float(ik["pos_err_m"]),
                        "ik_iterations": int(ik["iterations"]),
                    }
                state = self._move_goal(
                    cmd,
                    duration=step_duration,
                    wait=True,
                    timeout=remaining,
                    command_reference_joint_deg=command_reference_joint_deg,
                    trace_entry=trace_entry,
                )
                if trace_entry is not None:
                    trace_entry["after_tcp_pose"] = self._xyz_dict(state["tcp_pose"]["xyz"])
                    trace_steps.append(trace_entry)
                command_reference_joint_deg.update({name: float(value) for name, value in cmd.items()})
                q_seed_deg = self._state_to_arm_q_deg(state)

            final_state = self._hold_current_pose()
            if self.config.cartesian_settle_time_s > 0.0:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                settle_time = float(self.config.cartesian_settle_time_s)
                if remaining is not None:
                    settle_time = min(settle_time, remaining)
                if settle_time > 0.0:
                    time.sleep(settle_time)
                    final_state = self.get_state()
            final_err = self._tcp_position_error_m(final_state, target_pos)
            if final_err > self.config.max_ee_pos_err_m:
                raise IKError(
                    f"Cartesian move settled with {final_err:.6f} m position error "
                    f"(limit {self.config.max_ee_pos_err_m:.6f} m)"
                )
            return final_state
        except Exception as exc:
            if trace_steps is None:
                raise
            error_type = exc.__class__.__name__
            details = {
                "trace": self._finalize_trace(trace_steps),
                "last_state": state,
                "target_tcp": self._xyz_dict(target_pos),
                "start_tcp": self._xyz_dict(start_xyz),
                "error_type": error_type,
            }
            if isinstance(exc, IKTraceError):
                exc.details.update(details)
                raise
            raise IKTraceError(str(exc), details=details) from exc

    def _move_joint_targets_smooth(
        self,
        *,
        start_state: Dict[str, Any],
        target_cmd: Dict[str, float],
        duration: float,
        wait: bool,
        timeout: Optional[float],
        step_size: Optional[float] = None,
        trace_steps: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not wait:
            trace_entry = None
            if trace_steps is not None:
                trace_entry = {
                    "mode": "joint",
                    "step_index": 1,
                    "steps_total": 1,
                    "alpha": 1.0,
                }
            state = self._move_goal(
                target_cmd,
                duration=duration,
                wait=False,
                timeout=timeout,
                command_reference_joint_deg={name: float(start_state["joint_state"][name]) for name in JOINTS},
                trace_entry=trace_entry,
            )
            if trace_entry is not None:
                trace_steps.append(trace_entry)
            return state

        resolved_step_size = max(1e-4, float(step_size or self.config.joint_step_deg))
        max_change = max(
            abs(float(target_value) - float(start_state["joint_state"][joint_name]))
            for joint_name, target_value in target_cmd.items()
        )
        hz_steps = int(np.ceil(max(0.0, float(duration)) * max(1.0, float(self.config.joint_update_hz))))
        steps = max(1, int(np.ceil(max_change / resolved_step_size)), hz_steps)
        step_duration = max(0.0, float(duration)) / steps if steps else 0.0
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        state = start_state
        command_reference_joint_deg = {name: float(start_state["joint_state"][name]) for name in JOINTS}
        for step_index in range(1, steps + 1):
            alpha = self._smooth_fraction(float(step_index) / float(steps))
            waypoint_cmd = {
                joint_name: float(start_state["joint_state"][joint_name])
                + (float(target_value) - float(start_state["joint_state"][joint_name])) * alpha
                for joint_name, target_value in target_cmd.items()
            }
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            trace_entry = None
            if trace_steps is not None:
                trace_entry = {
                    "mode": "joint",
                    "step_index": int(step_index),
                    "steps_total": int(steps),
                    "alpha": float(alpha),
                }
            state = self._move_goal(
                waypoint_cmd,
                duration=step_duration,
                wait=True,
                timeout=remaining,
                command_reference_joint_deg=command_reference_joint_deg,
                trace_entry=trace_entry,
            )
            if trace_entry is not None:
                trace_steps.append(trace_entry)
            command_reference_joint_deg.update({name: float(value) for name, value in waypoint_cmd.items()})
        return self._hold_current_pose()

    @staticmethod
    def _xyz_dict(xyz: Any) -> Dict[str, float]:
        return {"x": float(xyz[0]), "y": float(xyz[1]), "z": float(xyz[2])}

    @staticmethod
    def _tcp_delta(before_state: Dict[str, Any], after_state: Dict[str, Any]) -> Dict[str, float]:
        before_xyz = before_state["tcp_pose"]["xyz"]
        after_xyz = after_state["tcp_pose"]["xyz"]
        return {
            "dx": float(after_xyz[0] - before_xyz[0]),
            "dy": float(after_xyz[1] - before_xyz[1]),
            "dz": float(after_xyz[2] - before_xyz[2]),
        }

    @staticmethod
    def _tcp_position_error_m(state: Dict[str, Any], target_pos: np.ndarray) -> float:
        current_xyz = np.asarray(state["tcp_pose"]["xyz"], dtype=float)
        return float(np.linalg.norm(np.asarray(target_pos, dtype=float) - current_xyz))

    @staticmethod
    def _smooth_fraction(fraction: float) -> float:
        t = min(1.0, max(0.0, float(fraction)))
        return t * t * (3.0 - 2.0 * t)

    @staticmethod
    def _finalize_trace(trace_steps: list[Dict[str, Any]]) -> Dict[str, Any]:
        if not trace_steps:
            return {"steps": [], "summary": {"steps": 0}}
        joint_names = list(JOINTS)
        max_abs_joint_error_deg = {
            name: max(abs(float(step.get("joint_error_deg", {}).get(name, 0.0))) for step in trace_steps)
            for name in joint_names
        }
        max_abs_raw_delta = {
            name: max(abs(int(step.get("raw_present_position_delta", {}).get(name, 0))) for step in trace_steps)
            for name in joint_names
        }
        return {
            "steps": trace_steps,
            "summary": {
                "steps": len(trace_steps),
                "max_abs_joint_error_deg": max_abs_joint_error_deg,
                "max_abs_raw_delta": max_abs_raw_delta,
            },
        }
