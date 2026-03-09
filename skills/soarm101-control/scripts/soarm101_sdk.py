#!/usr/bin/env python3
"""SDK-style control module for the local soarm101 follower arm."""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import draccus
import kinpy as kp
import numpy as np
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode
from scipy.spatial.transform import Rotation as R


JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]
ARM_JOINTS = JOINTS[:-1]
DEFAULT_URDF_PATH = "/home/sunyuan/Code/SoarMOCE/SO-ARM-Moce/Software/Master/so101.urdf"
DEFAULT_TARGET_FRAME = "gripper_frame_link"
DEFAULT_HOME_JOINTS = {
    "shoulder_pan": -8.923076923076923,
    "shoulder_lift": -9.31868131868132,
    "elbow_flex": 8.483516483516484,
    "wrist_flex": -3.6043956043956045,
    "wrist_roll": -0.17582417582417584,
    "gripper": 25.766470971950422,
}
__all__ = ["ARM_JOINTS", "JOINTS", "HardwareError", "IKError", "SoArm101Config", "SoArm101Controller", "ValidationError", "resolve_config", "to_jsonable"]


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
    arm_p_coefficient: int
    arm_d_coefficient: int
    max_ee_pos_err_m: float
    max_ee_ang_err_rad: float
    linear_step_m: float
    joint_step_deg: float


def _load_calibration(robot_name: str, calib_dir: Path) -> dict[str, MotorCalibration]:
    fpath = calib_dir / f"{robot_name}.json"
    if not fpath.exists():
        raise FileNotFoundError(f"Calibration file not found: {fpath}")
    with open(fpath, "r", encoding="utf-8") as f, draccus.config_type("json"):
        return draccus.load(dict[str, MotorCalibration], f)


def _candidate_calibration_dirs() -> list[Path]:
    env = os.environ.get("SOARM101_CALIB_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            Path("/home/sunyuan/Code/SoarMOCE/SO-ARM-Moce/Software/Slave/calibration/robots/so101_follower"),
            Path("/home/sunyuan/Code/SoarMOCE/SO-ARM-Moce/Software/Master/calibration/robots/so101_follower"),
            Path.cwd() / "Software/Slave/calibration/robots/so101_follower",
            Path.home() / "Code/SoarMOCE/SO-ARM-Moce/Software/Slave/calibration/robots/so101_follower",
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


def _resolve_home_joints() -> Dict[str, float]:
    raw = os.environ.get("SOARM101_HOME_JOINTS_JSON", "").strip()
    if not raw:
        return {name: float(value) for name, value in DEFAULT_HOME_JOINTS.items()}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in SOARM101_HOME_JOINTS_JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("SOARM101_HOME_JOINTS_JSON must be a JSON object")
    home_joints = {name: float(value) for name, value in DEFAULT_HOME_JOINTS.items()}
    for joint_name, joint_value in payload.items():
        joint = str(joint_name).strip()
        if joint not in JOINTS:
            raise ValidationError(f"Unknown joint in SOARM101_HOME_JOINTS_JSON: {joint}")
        if not isinstance(joint_value, (int, float)):
            raise ValidationError(f"Home joint value for {joint} must be numeric")
        home_joints[joint] = float(joint_value)
    return home_joints


def resolve_config() -> SoArm101Config:
    robot_id = str(os.environ.get("SOARM101_ROBOT_ID", "brown_arm_follower") or "brown_arm_follower").strip()
    port = str(os.environ.get("SOARM101_PORT", "/dev/ttyACM0") or "/dev/ttyACM0").strip()
    urdf_path = Path(str(os.environ.get("SOARM101_URDF_PATH", DEFAULT_URDF_PATH) or DEFAULT_URDF_PATH)).expanduser()
    target_frame = str(os.environ.get("SOARM101_TARGET_FRAME", DEFAULT_TARGET_FRAME) or DEFAULT_TARGET_FRAME).strip()

    chosen_dir = None
    for candidate in _candidate_calibration_dirs():
        if (candidate / f"{robot_id}.json").exists():
            chosen_dir = candidate.resolve()
            break
    if chosen_dir is None:
        searched = [str(path) for path in _candidate_calibration_dirs()]
        raise FileNotFoundError(
            f"Could not find calibration for {robot_id}. Searched: {searched}. Set SOARM101_CALIB_DIR explicitly."
        )

    return SoArm101Config(
        port=port,
        robot_id=robot_id,
        calib_dir=chosen_dir,
        urdf_path=urdf_path,
        target_frame=target_frame,
        home_joints=_resolve_home_joints(),
        arm_p_coefficient=int(os.environ.get("SOARM101_ARM_P_COEFFICIENT", "16")),
        arm_d_coefficient=int(os.environ.get("SOARM101_ARM_D_COEFFICIENT", "8")),
        max_ee_pos_err_m=float(os.environ.get("SOARM101_MAX_EE_POS_ERR_M", "0.03")),
        max_ee_ang_err_rad=float(os.environ.get("SOARM101_MAX_EE_ANG_ERR_RAD", "0.05")),
        linear_step_m=float(os.environ.get("SOARM101_LINEAR_STEP_M", "0.01")),
        joint_step_deg=float(os.environ.get("SOARM101_JOINT_STEP_DEG", "5.0")),
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
            "robot_id": self.config.robot_id,
            "port": self.config.port,
        }

    def get_state(self) -> Dict[str, Any]:
        bus = self._ensure_bus()
        current = bus.sync_read("Present_Position")
        joints = {name: float(current.get(name, 0.0)) for name in JOINTS}
        gripper_value = max(0.0, min(100.0, joints["gripper"]))
        tf = self._forward_kinematics_from_arm_deg(np.array([joints[name] for name in ARM_JOINTS], dtype=float))
        pose = self._transform_to_pose_dict(tf)
        pose.pop("rot_matrix", None)
        return {
            "joint_state": joints,
            "tcp_pose": pose,
            "gripper": {
                "value_0_100": gripper_value,
                "open_ratio": gripper_value / 100.0,
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
        if joint_name == "gripper":
            raise ValidationError("Use set_gripper/open_gripper/close_gripper instead of move_joint on gripper")
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
            if joint == "gripper":
                raise ValidationError("Use set_gripper for gripper; move_joints only supports arm joints")
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
        if not (0.0 <= float(open_ratio) <= 1.0):
            raise ValidationError("open_ratio must be within [0.0, 1.0]")
        before = self.get_state()
        state = self._move_joint_targets_smooth(
            start_state=before,
            target_cmd={"gripper": float(open_ratio) * 100.0},
            duration=duration,
            wait=wait,
            timeout=timeout,
            step_size=max(1.0, self.config.joint_step_deg * 2.0),
        )
        return {
            "action": "set_gripper",
            "open_ratio": float(open_ratio),
            "state": state,
        }

    def open_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        return self.set_gripper(open_ratio=1.0, duration=duration, wait=wait, timeout=timeout)

    def close_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        return self.set_gripper(open_ratio=0.0, duration=duration, wait=wait, timeout=timeout)

    def stop(self) -> Dict[str, Any]:
        state = self._hold_current_pose()
        return {"action": "stop", "held": True, "state": state}

    def _ensure_bus(self) -> FeetechMotorsBus:
        with self._lock:
            if self._bus is not None:
                return self._bus
            calib = _load_calibration(self.config.robot_id, self.config.calib_dir)
            bus = FeetechMotorsBus(
                port=self.config.port,
                motors={
                    "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
                    "shoulder_lift": Motor(2, "sts3215", MotorNormMode.DEGREES),
                    "elbow_flex": Motor(3, "sts3215", MotorNormMode.DEGREES),
                    "wrist_flex": Motor(4, "sts3215", MotorNormMode.DEGREES),
                    "wrist_roll": Motor(5, "sts3215", MotorNormMode.DEGREES),
                    "gripper": Motor(6, "sts3215", MotorNormMode.RANGE_0_100),
                },
                calibration=calib,
            )
            bus.connect()
            with bus.torque_disabled():
                bus.configure_motors()
                for name in JOINTS:
                    bus.write("Operating_Mode", name, OperatingMode.POSITION.value)
                    if name != "gripper":
                        bus.write("P_Coefficient", name, self.config.arm_p_coefficient)
                        bus.write("I_Coefficient", name, 0)
                        bus.write("D_Coefficient", name, self.config.arm_d_coefficient)
            bus.enable_torque()
            current = bus.sync_read("Present_Position")
            bus.sync_write("Goal_Position", {name: float(current[name]) for name in JOINTS if name in current})
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

    def _move_goal(self, cmd: Dict[str, float], *, duration: float, wait: bool, timeout: Optional[float]) -> Dict[str, Any]:
        bus = self._ensure_bus()
        bus.sync_write("Goal_Position", {name: float(value) for name, value in cmd.items()})
        self._wait(duration, wait, timeout)
        return self.get_state()

    def _hold_current_pose(self) -> Dict[str, Any]:
        state = self.get_state()
        hold = {name: float(state["joint_state"][name]) for name in JOINTS}
        self._ensure_bus().sync_write("Goal_Position", hold)
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
