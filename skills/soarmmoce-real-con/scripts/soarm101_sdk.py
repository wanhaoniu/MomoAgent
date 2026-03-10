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
from scipy.spatial.transform import Rotation as R


REPO_ROOT = Path(__file__).resolve().parents[3]
JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
]
ARM_JOINTS = list(JOINTS)
MULTI_TURN_JOINTS = ("shoulder_lift", "elbow_flex")
MULTI_TURN_RAW_RANGE = 900000
RAW_COUNTS_PER_REV = 4096.0
DEFAULT_TARGET_FRAME = "gripper_frame_link"
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
    "SoArm101Config",
    "SoArm101Controller",
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


@dataclass(frozen=True)
class SoArm101Config:
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
    max_ee_ang_err_rad: float
    linear_step_m: float
    joint_step_deg: float


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
    env = _env_value("SOARMMOCE_CALIB_DIR", "SOARM101_CALIB_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
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


def _candidate_urdf_paths() -> list[Path]:
    env = _env_value("SOARMMOCE_URDF_PATH", "SOARM101_URDF_PATH")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            REPO_ROOT / "Software/Master/so101.urdf",
            Path.cwd() / "Software/Master/so101.urdf",
            Path.home() / "Code/SO-ARM-Moce/Software/Master/so101.urdf",
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
    raw = _env_value("SOARMMOCE_HOME_JOINTS_JSON", "SOARM101_HOME_JOINTS_JSON")
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
    raw = _env_value("SOARMMOCE_JOINT_SCALE_JSON", "SOARM101_JOINT_SCALE_JSON")
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


def resolve_config() -> SoArm101Config:
    robot_id = _env_value("SOARMMOCE_ROBOT_ID", "SOARM101_ROBOT_ID", default="follower_moce")
    port = _env_value("SOARMMOCE_PORT", "SOARM101_PORT", default="/dev/ttyACM0")
    urdf_path = _resolve_urdf_path()
    target_frame = _env_value("SOARMMOCE_TARGET_FRAME", "SOARM101_TARGET_FRAME", default=DEFAULT_TARGET_FRAME)

    chosen_dir = None
    for candidate in _candidate_calibration_dirs():
        if (candidate / f"{robot_id}.json").exists():
            chosen_dir = candidate.resolve()
            break
    if chosen_dir is None:
        searched = [str(path) for path in _candidate_calibration_dirs()]
        raise FileNotFoundError(
            f"Could not find calibration for {robot_id}. Searched: {searched}. Set SOARMMOCE_CALIB_DIR explicitly."
        )

    return SoArm101Config(
        port=port,
        robot_id=robot_id,
        calib_dir=chosen_dir,
        urdf_path=urdf_path,
        target_frame=target_frame,
        home_joints=_resolve_home_joints(),
        joint_scales=_resolve_joint_scales(),
        arm_p_coefficient=int(
            _env_value("SOARMMOCE_ARM_P_COEFFICIENT", "SOARM101_ARM_P_COEFFICIENT", default="16")
        ),
        arm_d_coefficient=int(
            _env_value("SOARMMOCE_ARM_D_COEFFICIENT", "SOARM101_ARM_D_COEFFICIENT", default="8")
        ),
        max_ee_pos_err_m=float(
            _env_value("SOARMMOCE_MAX_EE_POS_ERR_M", "SOARM101_MAX_EE_POS_ERR_M", default="0.03")
        ),
        max_ee_ang_err_rad=float(
            _env_value("SOARMMOCE_MAX_EE_ANG_ERR_RAD", "SOARM101_MAX_EE_ANG_ERR_RAD", default="0.05")
        ),
        linear_step_m=float(_env_value("SOARMMOCE_LINEAR_STEP_M", "SOARM101_LINEAR_STEP_M", default="0.01")),
        joint_step_deg=float(_env_value("SOARMMOCE_JOINT_STEP_DEG", "SOARM101_JOINT_STEP_DEG", default="5.0")),
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


class SoArm101Controller:
    def __init__(self, config: Optional[SoArm101Config] = None):
        self.config = config or resolve_config()
        self._lock = threading.Lock()
        self._bus: Optional[FeetechMotorsBus] = None
        self._kin_chain = None

    def __enter__(self) -> "SoArm101Controller":
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

    def meta(self) -> Dict[str, Any]:
        return {
            "connected": True,
            "robot_type": "soarmmoce",
            "robot_id": self.config.robot_id,
            "port": self.config.port,
            "joint_names": list(JOINTS),
            "multi_turn_joints": list(MULTI_TURN_JOINTS),
            "joint_scales": dict(self.config.joint_scales),
            "gripper_available": False,
        }

    def get_state(self) -> Dict[str, Any]:
        bus = self._ensure_bus()
        current_motor = bus.sync_read("Present_Position")
        joints = {name: self._motor_to_joint_deg(name, float(current_motor.get(name, 0.0))) for name in JOINTS}
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

    def move_to(
        self,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
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
        state = self._move_tcp_smooth(
            start_state=before,
            target_pos=target_pos,
            target_rot=current_tf.rot,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return {
            "action": "move_to",
            "target_tcp": self._xyz_dict(target_pos),
            "tcp_delta": self._tcp_delta(before, state),
            "state": state,
        }

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
        state = self._move_tcp_smooth(
            start_state=before,
            target_pos=target_pos,
            target_rot=current_tf.rot,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return {
            "action": "move_delta",
            "requested_delta": {"dx": float(dx), "dy": float(dy), "dz": float(dz), "frame": frame},
            "tcp_delta": self._tcp_delta(before, state),
            "state": state,
        }

    def move_joint(
        self,
        *,
        joint: str,
        delta_deg: Optional[float] = None,
        target_deg: Optional[float] = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        joint_name = self._validate_joint_name(joint)
        if (delta_deg is None) == (target_deg is None):
            raise ValidationError("Exactly one of delta_deg or target_deg must be provided")
        before = self.get_state()
        current = float(before["joint_state"][joint_name])
        target = float(current + float(delta_deg)) if delta_deg is not None else float(target_deg)
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd={joint_name: target},
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return {
            "action": "move_joint",
            "joint": joint_name,
            "delta_deg": float(target - current),
            "target_deg": target,
            "state": state,
        }

    def move_joints(
        self,
        *,
        targets_deg: Dict[str, float],
        duration: float = 1.0,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not isinstance(targets_deg, dict) or not targets_deg:
            raise ValidationError("targets_deg must be a non-empty object")
        before = self.get_state()
        cmd: Dict[str, float] = {}
        for raw_joint, raw_value in targets_deg.items():
            joint = self._validate_joint_name(str(raw_joint))
            if not isinstance(raw_value, (int, float)):
                raise ValidationError(f"targets_deg.{joint} must be a number")
            cmd[joint] = float(raw_value)
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd=cmd,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return {
            "action": "move_joints",
            "targets_deg": cmd,
            "state": state,
        }

    def home(self, *, duration: float = 1.5, wait: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        before = self.get_state()
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd=dict(self.config.home_joints),
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return {"action": "home", "target_joints": dict(self.config.home_joints), "state": state}

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

    def _solve_ik_to_pose(self, target_tf: kp.Transform, q_seed_deg: np.ndarray) -> Dict[str, Any]:
        chain = self._ensure_kin_chain()
        q_seed_deg = np.asarray(q_seed_deg, dtype=float).reshape(-1)
        q_seed_rad = np.deg2rad(q_seed_deg)
        q_target_rad = np.asarray(chain.inverse_kinematics(target_tf, initial_state=q_seed_rad), dtype=float).reshape(-1)
        if q_target_rad.shape[0] != len(ARM_JOINTS) or not np.all(np.isfinite(q_target_rad)):
            raise IKError("IK solver returned invalid joint values")
        q_target_deg = np.rad2deg(q_target_rad)
        solved_tf = chain.forward_kinematics(q_target_rad)
        pos_err = float(np.linalg.norm(np.asarray(solved_tf.pos) - np.asarray(target_tf.pos)))
        r_target = R.from_quat([target_tf.rot[1], target_tf.rot[2], target_tf.rot[3], target_tf.rot[0]])
        r_solved = R.from_quat([solved_tf.rot[1], solved_tf.rot[2], solved_tf.rot[3], solved_tf.rot[0]])
        ang_err = float(np.linalg.norm((r_solved * r_target.inv()).as_rotvec()))
        if pos_err > self.config.max_ee_pos_err_m:
            raise IKError(
                f"IK position error too large: {pos_err:.6f} m (limit {self.config.max_ee_pos_err_m:.6f} m)"
            )
        if ang_err > self.config.max_ee_ang_err_rad:
            raise IKError(f"IK orientation error too large: {ang_err:.6f} rad")
        return {"q_target_deg": q_target_deg}

    def _joint_scale(self, joint_name: str) -> float:
        return float(self.config.joint_scales.get(joint_name, 1.0))

    def _motor_to_joint_deg(self, joint_name: str, motor_deg: float) -> float:
        return float(motor_deg) / self._joint_scale(joint_name)

    def _joint_to_motor_deg(self, joint_name: str, joint_deg: float) -> float:
        return float(joint_deg) * self._joint_scale(joint_name)

    def _read_joint_state_from_bus(self, bus: FeetechMotorsBus) -> Dict[str, float]:
        current_motor = bus.sync_read("Present_Position")
        return {name: self._motor_to_joint_deg(name, float(current_motor.get(name, 0.0))) for name in JOINTS}

    def _get_current_joint_state(self) -> Dict[str, float]:
        return self._read_joint_state_from_bus(self._ensure_bus())

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

    def _move_goal(self, cmd: Dict[str, float], *, duration: float, wait: bool, timeout: Optional[float]) -> Dict[str, Any]:
        bus = self._ensure_bus()
        current_joint_deg = self._read_joint_state_from_bus(bus)
        bus_cmd = self._build_bus_command(cmd, current_joint_deg=current_joint_deg)
        if bus_cmd:
            bus.sync_write("Goal_Position", bus_cmd)
        self._wait(duration, wait, timeout)
        return self.get_state()

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
        target_rot: Any,
        duration: float,
        wait: bool,
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        start_xyz = np.asarray(start_state["tcp_pose"]["xyz"], dtype=float)
        q_seed_deg = self._state_to_arm_q_deg(start_state)
        if not wait:
            ik = self._solve_ik_to_pose(kp.Transform(rot=target_rot, pos=target_pos), q_seed_deg)
            cmd = {name: float(ik["q_target_deg"][idx]) for idx, name in enumerate(ARM_JOINTS)}
            return self._move_goal(cmd, duration=duration, wait=False, timeout=timeout)

        step_m = max(1e-4, float(self.config.linear_step_m))
        distance = float(np.linalg.norm(target_pos - start_xyz))
        steps = max(1, int(np.ceil(distance / step_m)))
        step_duration = max(0.0, float(duration)) / steps if steps else 0.0
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        state = start_state
        for step_index in range(1, steps + 1):
            alpha = float(step_index) / float(steps)
            waypoint_pos = start_xyz + (target_pos - start_xyz) * alpha
            ik = self._solve_ik_to_pose(kp.Transform(rot=target_rot, pos=waypoint_pos), q_seed_deg)
            q_seed_deg = np.asarray(ik["q_target_deg"], dtype=float)
            cmd = {name: float(ik["q_target_deg"][idx]) for idx, name in enumerate(ARM_JOINTS)}
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            state = self._move_goal(cmd, duration=step_duration, wait=True, timeout=remaining)
        return self._hold_current_pose()

    def _move_joint_targets_smooth(
        self,
        *,
        start_state: Dict[str, Any],
        target_cmd: Dict[str, float],
        duration: float,
        wait: bool,
        timeout: Optional[float],
        step_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not wait:
            return self._move_goal(target_cmd, duration=duration, wait=False, timeout=timeout)

        resolved_step_size = max(1e-4, float(step_size or self.config.joint_step_deg))
        max_change = max(
            abs(float(target_value) - float(start_state["joint_state"][joint_name]))
            for joint_name, target_value in target_cmd.items()
        )
        steps = max(1, int(np.ceil(max_change / resolved_step_size)))
        step_duration = max(0.0, float(duration)) / steps if steps else 0.0
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        state = start_state
        for step_index in range(1, steps + 1):
            alpha = float(step_index) / float(steps)
            waypoint_cmd = {
                joint_name: float(start_state["joint_state"][joint_name])
                + (float(target_value) - float(start_state["joint_state"][joint_name])) * alpha
                for joint_name, target_value in target_cmd.items()
            }
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            state = self._move_goal(waypoint_cmd, duration=step_duration, wait=True, timeout=remaining)
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
