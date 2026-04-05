from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .json_utils import to_jsonable
from .kinematics_pybullet import PYBULLET_AVAILABLE, PYBULLET_IMPORT_ERROR, PybulletKinematicsModel


RAW_COUNTS_PER_REV = 4096
RAW_DEGREES_PER_REV = 360.0
HALF_RAW_COUNTS_PER_REV = RAW_COUNTS_PER_REV // 2
SINGLE_TURN_RAW_MIN = 0
SINGLE_TURN_RAW_MAX = RAW_COUNTS_PER_REV - 1
POSITION_MODE_VALUE = 0
MULTI_TURN_PHASE_VALUE = 28
MULTI_TURN_DISABLED_LIMIT_RAW = 0
MULTI_TURN_ABSOLUTE_RAW_LIMIT = 30719
DEFAULT_SERIAL_TIMEOUT_S = 2.0
DEFAULT_MOTOR_MODEL = "sts3215"
DEFAULT_LINEAR_STEP_M = 0.01
DEFAULT_JOINT_STEP_DEG = 5.0
DEFAULT_CARTESIAN_SETTLE_TIME_S = 0.15
DEFAULT_IK_JOINT_STEP_DEG = 8.0
DEFAULT_JOINT_SCALES = {
    "shoulder_pan": 1.0,
    "shoulder_lift": -5.3,
    "elbow_flex": 5.6,
    "wrist_flex": -1.0,
    "wrist_roll": 1.0,
}
# Model offsets are only for URDF/display-side zero alignment.
# They are not servo homing offsets and are not written to hardware registers.
DEFAULT_MODEL_OFFSETS_DEG = {
    "shoulder_pan": 0.0,
    "shoulder_lift": 0.0,
    "elbow_flex": 0.0,
    "wrist_flex": 0.0,
    "wrist_roll": 0.0,
}
DEFAULT_JOINT_NAME_ALIASES = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}
DEFAULT_MOTOR_IDS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
}
DEFAULT_JOINT_SETTLE_TOLERANCE_RAW = 16
DEFAULT_JOINT_POLL_INTERVAL_S = 0.02
DEFAULT_JOINT_WAIT_TIMEOUT_S = 2.5
GRIPPER_JOINT_NAME = "gripper"
DEFAULT_GRIPPER_SETTLE_TOLERANCE_RAW = 12
DEFAULT_GRIPPER_POLL_INTERVAL_S = 0.02
JOINTS = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
)
MULTI_TURN_JOINTS = ("shoulder_lift", "elbow_flex", "wrist_roll")
BOUNDED_SINGLE_TURN_JOINTS = tuple(joint_name for joint_name in JOINTS if joint_name not in MULTI_TURN_JOINTS)

PACKAGE_ROOT = Path(__file__).resolve().parent
SDK_ROOT = PACKAGE_ROOT.parent.parent
REPO_ROOT = SDK_ROOT.parent
SKILL_ROOT = REPO_ROOT / "skills" / "soarmmoce-real-con"
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "resources" / "configs" / "soarm_moce_serial.yaml"
DEFAULT_URDF_PATH = PACKAGE_ROOT / "resources" / "urdf" / "soarmoce_urdf.urdf"
DEFAULT_CALIB_DIR = SKILL_ROOT / "calibration"
DEFAULT_RUNTIME_DIR = SKILL_ROOT / "workspace" / "runtime"


class ValidationError(ValueError):
    pass


class HardwareError(RuntimeError):
    pass


class CapabilityError(RuntimeError):
    pass


class IKError(RuntimeError):
    pass


class AttrDict(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def _as_attrdict(value: Any) -> Any:
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, Mapping):
        return AttrDict({str(key): _as_attrdict(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_as_attrdict(item) for item in value]
    return value


def _pkg_path_to_fs(path: str | Path) -> Path:
    raw_path = str(path)
    if raw_path.startswith("pkg://soarmmoce_sdk/"):
        suffix = raw_path[len("pkg://soarmmoce_sdk/") :]
        return (PACKAGE_ROOT / suffix).resolve()
    return Path(raw_path).expanduser().resolve()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return loaded
    return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return loaded
    return {}


def _wrap_single_turn_raw(raw_value: int | float) -> int:
    return int(round(float(raw_value))) % RAW_COUNTS_PER_REV


def _signed_single_turn_delta(current_raw: int | float, startup_raw: int | float) -> int:
    delta = _wrap_single_turn_raw(current_raw) - _wrap_single_turn_raw(startup_raw)
    if delta > HALF_RAW_COUNTS_PER_REV:
        delta -= RAW_COUNTS_PER_REV
    elif delta < -HALF_RAW_COUNTS_PER_REV:
        delta += RAW_COUNTS_PER_REV
    return int(delta)


def _coerce_vector3(values: Iterable[Any] | None, *, name: str) -> list[float]:
    if values is None:
        raise ValidationError(f"{name} is required")
    payload = list(values)
    if len(payload) != 3:
        raise ValidationError(f"{name} must contain exactly 3 values, got {len(payload)}")
    return [float(payload[0]), float(payload[1]), float(payload[2])]


@dataclass(slots=True)
class SoArmMoceConfig:
    port: str = ""
    robot_id: str = "soarmmoce"
    calib_dir: Path = field(default_factory=lambda: DEFAULT_CALIB_DIR)
    urdf_path: Path = field(default_factory=lambda: DEFAULT_URDF_PATH)
    runtime_dir: Path = field(default_factory=lambda: DEFAULT_RUNTIME_DIR)
    target_frame: str = "wrist_roll"
    joint_scales: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_JOINT_SCALES))
    model_offsets_deg: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MODEL_OFFSETS_DEG))
    joint_name_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_JOINT_NAME_ALIASES))
    arm_p_coefficient: int = 16
    arm_d_coefficient: int = 8
    max_ee_pos_err_m: float = 0.03
    linear_step_m: float = DEFAULT_LINEAR_STEP_M
    joint_step_deg: float = DEFAULT_JOINT_STEP_DEG
    cartesian_settle_time_s: float = DEFAULT_CARTESIAN_SETTLE_TIME_S
    cartesian_update_hz: float = 25.0 #笛卡尔控制更新频率
    joint_update_hz: float = 25.0 #插值发送频率
    ik_target_tol_m: float = 0.02
    ik_max_iters: int = 200
    ik_damping: float = 0.05 #阻尼系数，增加数值会使IK解更平滑但可能无法完全收敛到目标
    ik_step_scale: float = 0.8 #IK每次迭代的步长缩放，过大可能导致震荡，过小则收敛过慢
    ik_joint_step_deg: float = DEFAULT_IK_JOINT_STEP_DEG
    ik_orientation_tol_rad: float = 0.02
    ik_seed_bias: float = 0.02
    gripper_available: bool = True
    serial_timeout_s: float = DEFAULT_SERIAL_TIMEOUT_S
    motor_model: str = DEFAULT_MOTOR_MODEL

    def __post_init__(self) -> None:
        self.calib_dir = Path(self.calib_dir).expanduser().resolve()
        self.urdf_path = Path(self.urdf_path).expanduser().resolve()
        self.runtime_dir = Path(self.runtime_dir).expanduser().resolve()
        self.port = str(self.port or "").strip()
        self.robot_id = str(self.robot_id or "soarmmoce").strip() or "soarmmoce"
        self.target_frame = str(self.target_frame or "wrist_roll").strip() or "wrist_roll"
        self.joint_scales = {joint: float(self.joint_scales.get(joint, DEFAULT_JOINT_SCALES[joint])) for joint in JOINTS}
        self.model_offsets_deg = {
            joint: float(self.model_offsets_deg.get(joint, DEFAULT_MODEL_OFFSETS_DEG[joint])) for joint in JOINTS
        }
        self.joint_name_aliases = {
            joint: str(self.joint_name_aliases.get(joint, DEFAULT_JOINT_NAME_ALIASES[joint]))
            for joint in JOINTS
        }

    @property
    def calibration_path(self) -> Path:
        return (self.calib_dir / f"{self.robot_id}.json").resolve()


@dataclass(frozen=True, slots=True)
class JointRuntimeState:
    startup_raw: int
    last_raw: int


@dataclass(frozen=True, slots=True)
class GripperSpec:
    name: str
    motor_id: int
    homing_offset: int
    range_min: int
    range_max: int


class JointPositionPolicy:
    kind: str = "base"

    def register_writes(self, calibration_entry: Mapping[str, Any]) -> list[tuple[str, int]]:
        raise NotImplementedError

    def normalize_present_raw(self, raw_value: int | float) -> int:
        raise NotImplementedError

    def relative_raw(self, *, current_raw: int, startup_raw: int) -> int:
        raise NotImplementedError

    def relative_to_absolute_goal_raw(self, *, startup_raw: int, relative_raw: int | float) -> int:
        raise NotImplementedError

    def validate_goal_raw(self, raw_value: int) -> None:
        return None


class SingleTurnPositionPolicy(JointPositionPolicy):
    kind = "single_turn"

    def register_writes(self, calibration_entry: Mapping[str, Any]) -> list[tuple[str, int]]:
        return [
            ("Homing_Offset", int(calibration_entry.get("homing_offset", 0))),
            ("Min_Position_Limit", int(calibration_entry.get("range_min", 0))),
            ("Max_Position_Limit", int(calibration_entry.get("range_max", RAW_COUNTS_PER_REV - 1))),
            ("Operating_Mode", POSITION_MODE_VALUE),
        ]

    def normalize_present_raw(self, raw_value: int | float) -> int:
        return _wrap_single_turn_raw(raw_value)

    def relative_raw(self, *, current_raw: int, startup_raw: int) -> int:
        return _signed_single_turn_delta(current_raw, startup_raw)

    def relative_to_absolute_goal_raw(self, *, startup_raw: int, relative_raw: int | float) -> int:
        return _wrap_single_turn_raw(startup_raw + int(round(float(relative_raw))))


class MultiTurnPositionPolicy(JointPositionPolicy):
    kind = "multi_turn"

    def register_writes(self, calibration_entry: Mapping[str, Any]) -> list[tuple[str, int]]:
        return [
            ("Homing_Offset", 0),
            ("Phase", int(calibration_entry.get("phase", MULTI_TURN_PHASE_VALUE))),
            ("Min_Position_Limit", MULTI_TURN_DISABLED_LIMIT_RAW),
            ("Max_Position_Limit", MULTI_TURN_DISABLED_LIMIT_RAW),
            ("Operating_Mode", POSITION_MODE_VALUE),
        ]

    def normalize_present_raw(self, raw_value: int | float) -> int:
        return int(round(float(raw_value)))

    def relative_raw(self, *, current_raw: int, startup_raw: int) -> int:
        return int(current_raw - startup_raw)

    def relative_to_absolute_goal_raw(self, *, startup_raw: int, relative_raw: int | float) -> int:
        goal_raw = int(round(float(startup_raw) + float(relative_raw)))
        self.validate_goal_raw(goal_raw)
        return goal_raw

    def validate_goal_raw(self, raw_value: int) -> None:
        if raw_value < -MULTI_TURN_ABSOLUTE_RAW_LIMIT or raw_value > MULTI_TURN_ABSOLUTE_RAW_LIMIT:
            raise HardwareError(
                "Requested multi-turn absolute goal is outside the hardware-supported range "
                f"[-{MULTI_TURN_ABSOLUTE_RAW_LIMIT}, {MULTI_TURN_ABSOLUTE_RAW_LIMIT}]: {raw_value}"
            )


SINGLE_TURN_POLICY = SingleTurnPositionPolicy()
MULTI_TURN_POLICY = MultiTurnPositionPolicy()


@dataclass(frozen=True, slots=True)
class JointSpec:
    name: str
    motor_id: int
    reduction_ratio: float
    policy: JointPositionPolicy


def resolve_config(path: str | Path | None = None) -> SoArmMoceConfig:
    config_path = Path(os.environ.get("SOARMMOCE_CONFIG", path or DEFAULT_CONFIG_PATH)).expanduser().resolve()
    payload = _load_yaml(config_path)
    transport = payload.get("transport", {}) if isinstance(payload.get("transport"), dict) else {}
    robot = payload.get("robot", {}) if isinstance(payload.get("robot"), dict) else {}
    control = payload.get("control", {}) if isinstance(payload.get("control"), dict) else {}
    ik = payload.get("ik", {}) if isinstance(payload.get("ik"), dict) else {}
    calibration = payload.get("calibration", {}) if isinstance(payload.get("calibration"), dict) else {}
    urdf = payload.get("urdf", {}) if isinstance(payload.get("urdf"), dict) else {}

    robot_id = str(os.environ.get("SOARMMOCE_ROBOT_ID", transport.get("robot_id", "soarmmoce"))).strip() or "soarmmoce"
    calib_dir = os.environ.get("SOARMMOCE_CALIB_DIR")
    if calib_dir:
        calib_root = Path(calib_dir).expanduser().resolve()
    else:
        calibration_path = str(calibration.get("path", "") or transport.get("calibration_path", "")).strip()
        if calibration_path:
            calib_root = _pkg_path_to_fs(calibration_path).expanduser().resolve().parent
        else:
            calib_root = DEFAULT_CALIB_DIR.resolve()

    port = str(os.environ.get("SOARMMOCE_PORT", transport.get("port", ""))).strip()
    urdf_path = _pkg_path_to_fs(str(urdf.get("path", DEFAULT_URDF_PATH)))

    return SoArmMoceConfig(
        port=port,
        robot_id=robot_id,
        calib_dir=calib_root,
        urdf_path=urdf_path,
        runtime_dir=DEFAULT_RUNTIME_DIR,
        target_frame=str(robot.get("end_link", "wrist_roll")),
        joint_scales={**DEFAULT_JOINT_SCALES, **dict(robot.get("joint_scales", {}))},
        model_offsets_deg={**DEFAULT_MODEL_OFFSETS_DEG, **dict(robot.get("sim_joint_offsets_deg", {}))},
        joint_name_aliases={**DEFAULT_JOINT_NAME_ALIASES, **dict(robot.get("joint_name_aliases", {}))},
        arm_p_coefficient=int(transport.get("arm_p_coefficient", 16)),
        arm_d_coefficient=int(transport.get("arm_d_coefficient", 8)),
        max_ee_pos_err_m=float(ik.get("max_pos_error_m", 0.03)),
        linear_step_m=float(control.get("linear_step_m", DEFAULT_LINEAR_STEP_M)),
        joint_step_deg=float(control.get("joint_step_deg", DEFAULT_JOINT_STEP_DEG)),
        cartesian_settle_time_s=float(control.get("cartesian_settle_time_s", DEFAULT_CARTESIAN_SETTLE_TIME_S)),
        cartesian_update_hz=float(control.get("hz", transport.get("update_hz", 25.0))),
        joint_update_hz=float(control.get("hz", transport.get("update_hz", 25.0))),
        ik_target_tol_m=float(ik.get("pos_tol", 0.001)),
        ik_max_iters=int(ik.get("max_iters", 200)),
        ik_damping=float(ik.get("damping", 0.05)),
        ik_step_scale=float(ik.get("step_scale", 0.8)),
        ik_joint_step_deg=float(ik.get("joint_step_deg", DEFAULT_IK_JOINT_STEP_DEG)),
        ik_orientation_tol_rad=float(ik.get("rot_tol", 0.02)),
        ik_seed_bias=float(ik.get("seed_bias", 0.02)),
        gripper_available=bool(transport.get("gripper_available", True)),
        serial_timeout_s=float(transport.get("timeout", DEFAULT_SERIAL_TIMEOUT_S)),
        motor_model=str(transport.get("motor_model", DEFAULT_MOTOR_MODEL)),
    )


class SoArmMoceController:
    """Real-arm controller rebuilt around startup-referenced position semantics.

    Important design points:
    - Single-turn and multi-turn joints both use operating mode 0. The difference
      is in how the servo registers are configured and how raw position values are
      interpreted, not in a separate runtime state machine.
    - Multi-turn joints disable both hardware position limits by writing 0/0, and
      set register 18 (Phase) to 28, which exposes the signed absolute range
      `-30719 .. 30719` required by the hardware.
    - Startup position is recorded once and used as the runtime zero reference.
      
    """

    def __init__(self, config: SoArmMoceConfig | None = None) -> None:
        self.config = config or resolve_config()
        self._calibration_payload = _read_json(self.config.calibration_path)
        self._joint_specs = self._build_joint_specs()
        self._gripper_spec = self._build_gripper_spec()
        self._joint_runtime_state: dict[str, JointRuntimeState] = {}
        self._multi_turn_state: dict[str, dict[str, float | int | None]] = {}
        self._last_multi_turn_goal_raw_mod: dict[str, int] = {}
        self._gripper_integrated = False
        self._gripper_probe_result: bool | None = None
        self._last_gripper_goal_raw: int | None = None
        self._manual_multi_turn_readback = False
        self._kinematics: PybulletKinematicsModel | None = None
        self._kinematics_init_attempted = False
        self._kinematics_error_text = ""
        self._bus: Any | None = None
        self.robot_model = _as_attrdict(
            {
                "joint_names": list(JOINTS),
                "joint_limits": [
                    (-2.0 * math.pi, 2.0 * math.pi) if joint in MULTI_TURN_JOINTS else (-math.pi, math.pi)
                    for joint in JOINTS
                ],
            }
        )

    def __enter__(self) -> SoArmMoceController:
        self._ensure_bus()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self, *, disable_torque: bool = True) -> None:
        if self._bus is not None:
            disconnect = getattr(self._bus, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(disable_torque=bool(disable_torque))
                except Exception:
                    try:
                        disconnect()
                    except Exception:
                        pass
            self._bus = None
            self._gripper_integrated = False

        if self._kinematics is not None:
            try:
                self._kinematics.close()
            except Exception:
                pass
        self._kinematics = None
        self._kinematics_init_attempted = False

    def _build_joint_specs(self) -> dict[str, JointSpec]:
        specs: dict[str, JointSpec] = {}
        for joint_name in JOINTS:
            if joint_name in MULTI_TURN_JOINTS:
                policy = MULTI_TURN_POLICY
            elif joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                policy = SINGLE_TURN_POLICY
            else:
                raise ValidationError(f"Joint '{joint_name}' is not assigned to a position policy group")
            specs[joint_name] = JointSpec(
                name=joint_name,
                motor_id=DEFAULT_MOTOR_IDS[joint_name],
                reduction_ratio=float(self.config.joint_scales[joint_name]),
                policy=policy,
            )
        return specs

    def _ensure_kinematics(self, *, required: bool = False) -> PybulletKinematicsModel | None:
        if self._kinematics is not None:
            return self._kinematics

        if not self._kinematics_init_attempted:
            self._kinematics_init_attempted = True
            try:
                self._kinematics = PybulletKinematicsModel(
                    urdf_path=self.config.urdf_path,
                    sdk_joint_names=JOINTS,
                    joint_name_aliases=self.config.joint_name_aliases,
                    model_offsets_deg=self.config.model_offsets_deg,
                    target_frame=self.config.target_frame,
                )
                self._kinematics_error_text = ""
            except Exception as exc:
                self._kinematics = None
                self._kinematics_error_text = str(exc).strip() or exc.__class__.__name__

        if required and self._kinematics is None:
            base_message = "Cartesian motion requires the optional PyBullet URDF kinematics backend."
            if not PYBULLET_AVAILABLE and PYBULLET_IMPORT_ERROR is not None:
                raise CapabilityError(f"{base_message} Import error: {PYBULLET_IMPORT_ERROR}") from PYBULLET_IMPORT_ERROR
            if self._kinematics_error_text:
                raise CapabilityError(f"{base_message} {self._kinematics_error_text}")
            raise CapabilityError(base_message)
        return self._kinematics

    def _compute_tcp_pose_from_joint_q(self, q_rad: Iterable[Any]) -> tuple[list[float], list[float]]:
        kinematics = self._ensure_kinematics(required=False)
        if kinematics is None:
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

        try:
            fk = kinematics.forward([float(value) for value in q_rad])
            return [float(value) for value in fk.xyz.tolist()], [float(value) for value in fk.rpy.tolist()]
        except Exception as exc:
            self._kinematics_error_text = str(exc).strip() or exc.__class__.__name__
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

    def get_end_effector_pose(self, q: Iterable[Any] | None = None) -> AttrDict:
        if q is None:
            state = self.get_state()
            return _as_attrdict(
                {
                    "xyz": list(state["tcp_pose"]["xyz"]),
                    "rpy": list(state["tcp_pose"]["rpy"]),
                }
            )

        xyz, rpy = self._compute_tcp_pose_from_joint_q([float(value) for value in list(q)])
        return _as_attrdict({"xyz": xyz, "rpy": rpy})

    def _build_gripper_spec(self) -> GripperSpec | None:
        if not bool(self.config.gripper_available):
            return None

        entry = self._calibration_payload.get(GRIPPER_JOINT_NAME)
        if entry is None:
            return None
        if not isinstance(entry, Mapping):
            raise ValidationError(
                f"Calibration entry '{GRIPPER_JOINT_NAME}' must be an object in {self.config.calibration_path}"
            )

        required_fields = ("id", "homing_offset", "range_min", "range_max")
        missing_fields = [field_name for field_name in required_fields if field_name not in entry]
        if missing_fields:
            raise ValidationError(
                f"Calibration entry '{GRIPPER_JOINT_NAME}' is missing required fields: {', '.join(missing_fields)}"
            )

        return GripperSpec(
            name=GRIPPER_JOINT_NAME,
            motor_id=int(entry["id"]),
            homing_offset=int(entry["homing_offset"]),
            range_min=int(entry["range_min"]),
            range_max=int(entry["range_max"]),
        )

    def _probe_gripper_presence(self) -> bool:
        if self._gripper_spec is None:
            return False
        if self._gripper_probe_result is not None:
            return bool(self._gripper_probe_result)

        try:
            from lerobot.motors import Motor, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus
        except ImportError:
            self._gripper_probe_result = False
            return False

        probe_bus = None
        detected = False
        try:
            probe_bus = FeetechMotorsBus(
                port=self.config.port,
                motors={
                    self._gripper_spec.name: Motor(
                        self._gripper_spec.motor_id,
                        self.config.motor_model,
                        MotorNormMode.DEGREES,
                    )
                },
            )
            connect = getattr(probe_bus, "connect", None)
            if callable(connect):
                connect()
            probe_bus.read("Present_Position", self._gripper_spec.name, normalize=False)
            detected = True
        except Exception:
            detected = False
        finally:
            disconnect = getattr(probe_bus, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass

        self._gripper_probe_result = bool(detected)
        return bool(detected)

    def _ensure_bus(self):
        if self._bus is not None:
            return self._bus

        try:
            from lerobot.motors import Motor, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus
        except ImportError as exc:
            raise HardwareError(
                "Real arm transport requires the optional 'lerobot' dependency, which is not installed in this Python environment."
            ) from exc

        motors = {
            joint_name: Motor(spec.motor_id, self.config.motor_model, MotorNormMode.DEGREES)
            for joint_name, spec in self._joint_specs.items()
        }
        self._gripper_integrated = False
        if self._probe_gripper_presence():
            motors[self._gripper_spec.name] = Motor(
                self._gripper_spec.motor_id,
                self.config.motor_model,
                MotorNormMode.DEGREES,
            )
            self._gripper_integrated = True
        bus = FeetechMotorsBus(port=self.config.port, motors=motors)
        connect = getattr(bus, "connect", None)
        if callable(connect):
            connect()

        self._bus = bus
        self._apply_position_mode_registers(bus)
        self._apply_gripper_registers(bus)
        self._prime_startup_references_from_current_pose(bus)
        self.apply_hold_state(self.capture_hold_state(bus), bus=bus)
        return bus

    def _apply_position_mode_registers(self, bus) -> None:
        for joint_name, spec in self._joint_specs.items():
            calibration_entry = self._calibration_payload.get(joint_name, {})
            for register_name, register_value in spec.policy.register_writes(calibration_entry):
                bus.write(register_name, joint_name, int(register_value), normalize=False)
        time.sleep(0.02)

    def _apply_gripper_registers(self, bus) -> None:
        if self._gripper_spec is None or not self._gripper_integrated:
            return
        # The gripper stays in the same single-turn position mode used by the arm.
        # For integrated pose record/replay we command the gripper by the raw
        # register values captured from Present_Position. Some historical gripper
        # calibration files contain very narrow range_min/range_max values for
        # standalone ratio-based control, which would block valid replay targets.
        # So the integrated controller keeps the homing offset, but widens the
        # hardware position limits to the full single-turn register range.
        bus.write("Operating_Mode", self._gripper_spec.name, POSITION_MODE_VALUE, normalize=False)
        bus.write("Homing_Offset", self._gripper_spec.name, int(self._gripper_spec.homing_offset), normalize=False)
        bus.write("Min_Position_Limit", self._gripper_spec.name, SINGLE_TURN_RAW_MIN, normalize=False)
        bus.write("Max_Position_Limit", self._gripper_spec.name, SINGLE_TURN_RAW_MAX, normalize=False)

    def _prime_startup_references_from_current_pose(self, bus) -> dict[str, int]:
        raw_present = self._read_raw_present_position(bus)
        self._joint_runtime_state = {
            joint_name: JointRuntimeState(
                startup_raw=self._joint_specs[joint_name].policy.normalize_present_raw(raw_present[joint_name]),
                last_raw=self._joint_specs[joint_name].policy.normalize_present_raw(raw_present[joint_name]),
            )
            for joint_name in JOINTS
        }
        self._multi_turn_state = self._build_multi_turn_state(raw_present)
        return raw_present

    def _prime_multi_turn_state_from_current_pose(self, bus) -> dict[str, int]:
        return self._prime_startup_references_from_current_pose(bus)

    def _read_raw_present_position(self, bus=None) -> dict[str, int]:
        active_bus = bus or self._ensure_bus()
        sync_read = getattr(active_bus, "sync_read", None)
        if callable(sync_read):
            try:
                payload = sync_read("Present_Position", normalize=False)
                if isinstance(payload, Mapping):
                    return {joint_name: int(payload[joint_name]) for joint_name in JOINTS}
            except Exception:
                pass
        return {
            joint_name: int(active_bus.read("Present_Position", joint_name, normalize=False))
            for joint_name in JOINTS
        }

    def _current_relative_raw_from_raw(self, joint_name: str, present_raw: int | float) -> int:
        spec = self._joint_specs[joint_name]
        normalized_raw = spec.policy.normalize_present_raw(present_raw)
        runtime_state = self._joint_runtime_state.get(joint_name)
        if runtime_state is None:
            runtime_state = JointRuntimeState(startup_raw=normalized_raw, last_raw=normalized_raw)
        relative_raw = spec.policy.relative_raw(current_raw=normalized_raw, startup_raw=runtime_state.startup_raw)
        self._joint_runtime_state[joint_name] = JointRuntimeState(
            startup_raw=runtime_state.startup_raw,
            last_raw=normalized_raw,
        )
        return int(relative_raw)

    def _relative_raw_to_motor_deg(self, relative_raw: int | float) -> float:
        return float(relative_raw) * RAW_DEGREES_PER_REV / float(RAW_COUNTS_PER_REV)

    def _relative_raw_to_joint_deg(self, joint_name: str, relative_raw: int | float) -> float:
        reduction_ratio = float(self._joint_specs[joint_name].reduction_ratio)
        if abs(reduction_ratio) < 1e-9:
            raise ValidationError(f"Joint {joint_name} has an invalid reduction ratio of 0")
        return self._relative_raw_to_motor_deg(relative_raw) / reduction_ratio

    def _joint_deg_to_relative_raw(self, joint_name: str, joint_deg: int | float) -> float:
        reduction_ratio = float(self._joint_specs[joint_name].reduction_ratio)
        if abs(reduction_ratio) < 1e-9:
            raise ValidationError(f"Joint {joint_name} has an invalid reduction ratio of 0")
        motor_deg = float(joint_deg) * reduction_ratio
        return motor_deg * float(RAW_COUNTS_PER_REV) / RAW_DEGREES_PER_REV

    def _relative_raw_to_absolute_goal_raw(self, joint_name: str, relative_raw: int | float) -> int:
        runtime_state = self._joint_runtime_state.get(joint_name)
        if runtime_state is None:
            raise HardwareError(f"Startup reference for joint '{joint_name}' is not initialized")
        spec = self._joint_specs[joint_name]
        goal_raw = spec.policy.relative_to_absolute_goal_raw(
            startup_raw=runtime_state.startup_raw,
            relative_raw=relative_raw,
        )
        spec.policy.validate_goal_raw(goal_raw)
        return int(goal_raw)

    def _joint_deg_to_absolute_goal_raw(self, joint_name: str, joint_deg: int | float) -> int:
        return self._relative_raw_to_absolute_goal_raw(joint_name, self._joint_deg_to_relative_raw(joint_name, joint_deg))

    def _multi_turn_raw_to_joint_deg(self, joint_name: str, raw_value: int | float) -> float:
        if joint_name not in MULTI_TURN_JOINTS:
            raise ValidationError(f"Joint '{joint_name}' is not configured as multi-turn")
        return self._relative_raw_to_joint_deg(joint_name, self._current_relative_raw_from_raw(joint_name, raw_value))

    def _continuous_raw_to_multi_turn_goal_raw(self, joint_name: str, continuous_raw: int | float) -> int:
        if joint_name not in MULTI_TURN_JOINTS:
            raise ValidationError(f"Joint '{joint_name}' is not configured as multi-turn")
        return self._relative_raw_to_absolute_goal_raw(joint_name, continuous_raw)

    def _joint_deg_to_multi_turn_goal_raw(self, joint_name: str, joint_deg: int | float) -> int:
        if joint_name not in MULTI_TURN_JOINTS:
            raise ValidationError(f"Joint '{joint_name}' is not configured as multi-turn")
        return self._joint_deg_to_absolute_goal_raw(joint_name, joint_deg)

    def _build_multi_turn_state(self, raw_present: Mapping[str, int]) -> dict[str, dict[str, float | int | None]]:
        snapshot: dict[str, dict[str, float | int | None]] = {}
        for joint_name in MULTI_TURN_JOINTS:
            relative_raw = self._current_relative_raw_from_raw(joint_name, raw_present[joint_name])
            runtime_state = self._joint_runtime_state[joint_name]
            snapshot[joint_name] = {
                "startup_raw": int(runtime_state.startup_raw),
                "current_raw": int(raw_present[joint_name]),
                "continuous_raw": int(relative_raw),
                "relative_raw": int(relative_raw),
                "motor_deg": float(self._relative_raw_to_motor_deg(relative_raw)),
                "joint_deg": float(self._relative_raw_to_joint_deg(joint_name, relative_raw)),
                "goal_raw": self._last_multi_turn_goal_raw_mod.get(joint_name),
            }
        return snapshot

    def _snapshot_multi_turn_state(self) -> dict[str, dict[str, float | int | None]]:
        raw_present = self._read_raw_present_position()
        self._multi_turn_state = self._build_multi_turn_state(raw_present)
        return to_jsonable(self._multi_turn_state)

    def set_manual_multi_turn_readback(self, enabled: bool) -> None:
        # The old readback mode was tied to continuous turn accumulation. The new
        # design keeps the flag for compatibility but does not alter semantics.
        self._manual_multi_turn_readback = bool(enabled)

    def has_gripper(self) -> bool:
        self._ensure_bus()
        return bool(self._gripper_integrated and self._gripper_spec is not None)

    def _require_gripper_spec(self) -> GripperSpec:
        if self._gripper_spec is None or not self.has_gripper():
            raise CapabilityError("Optional gripper is not available on this controller.")
        return self._gripper_spec

    def _read_gripper_register_raw(self, bus=None) -> int | None:
        if self._gripper_spec is None or not self._gripper_integrated:
            return None
        active_bus = bus or self._ensure_bus()
        return int(active_bus.read("Present_Position", self._gripper_spec.name, normalize=False))

    def _gripper_register_raw_to_adjusted_raw(self, register_raw: int | float) -> int:
        spec = self._require_gripper_spec()
        return _wrap_single_turn_raw(int(round(float(register_raw))) + int(spec.homing_offset))

    def _gripper_adjusted_raw_to_register_raw(self, adjusted_raw: int | float) -> int:
        spec = self._require_gripper_spec()
        return _wrap_single_turn_raw(int(round(float(adjusted_raw))) - int(spec.homing_offset))

    def _gripper_adjusted_raw_to_open_ratio(self, adjusted_raw: int | float) -> float:
        spec = self._require_gripper_spec()
        span = float(spec.range_max - spec.range_min)
        if abs(span) <= 1e-9:
            return 0.0
        ratio = (float(adjusted_raw) - float(spec.range_min)) / span
        return float(min(1.0, max(0.0, ratio)))

    def _open_ratio_to_gripper_adjusted_raw(self, open_ratio: float) -> int:
        spec = self._require_gripper_spec()
        ratio = min(1.0, max(0.0, float(open_ratio)))
        return int(round(float(spec.range_min) + ratio * float(spec.range_max - spec.range_min)))

    def _open_ratio_to_gripper_goal_raw(self, open_ratio: float) -> int:
        adjusted_raw = self._open_ratio_to_gripper_adjusted_raw(open_ratio)
        return self._gripper_adjusted_raw_to_register_raw(adjusted_raw)

    def _build_gripper_state(self, bus=None) -> AttrDict | None:
        if self._gripper_spec is None or not self._gripper_integrated:
            return None
        register_raw = self._read_gripper_register_raw(bus)
        if register_raw is None:
            return None
        adjusted_raw = self._gripper_register_raw_to_adjusted_raw(register_raw)
        return _as_attrdict(
            {
                "available": True,
                "present_raw": int(register_raw),
                "present_register_raw": int(register_raw),
                "adjusted_raw": int(adjusted_raw),
                "open_ratio": float(self._gripper_adjusted_raw_to_open_ratio(adjusted_raw)),
                "goal_raw": self._last_gripper_goal_raw,
                "range_min": int(self._gripper_spec.range_min),
                "range_max": int(self._gripper_spec.range_max),
                "homing_offset": int(self._gripper_spec.homing_offset),
            }
        )

    def get_gripper_state(self) -> AttrDict | None:
        self._ensure_bus()
        return self._build_gripper_state()

    def read_gripper_raw(self) -> int | None:
        state = self.get_gripper_state()
        if state is None:
            return None
        return int(state["present_raw"])

    def _build_state(self, raw_present: Mapping[str, int]) -> AttrDict:
        joint_state_deg: dict[str, float] = {}
        joint_state_rad: list[float] = []
        raw_relative: dict[str, int] = {}
        startup_raw: dict[str, int] = {}
        motor_deg: dict[str, float] = {}
        output_deg: dict[str, float] = {}

        for joint_name in JOINTS:
            relative_raw = self._current_relative_raw_from_raw(joint_name, raw_present[joint_name])
            runtime_state = self._joint_runtime_state[joint_name]
            joint_deg = self._relative_raw_to_joint_deg(joint_name, relative_raw)
            joint_state_deg[joint_name] = float(joint_deg)
            joint_state_rad.append(math.radians(float(joint_deg)))
            raw_relative[joint_name] = int(relative_raw)
            startup_raw[joint_name] = int(runtime_state.startup_raw)
            motor_deg[joint_name] = float(self._relative_raw_to_motor_deg(relative_raw))
            output_deg[joint_name] = float(joint_deg)

        tcp_xyz, tcp_rpy = self._compute_tcp_pose_from_joint_q(joint_state_rad)
        self._multi_turn_state = self._build_multi_turn_state(raw_present)
        gripper_state = self._build_gripper_state()

        state = {
            "joint_state": {
                **joint_state_deg,
                "names": list(JOINTS),
                "values_deg": [joint_state_deg[joint_name] for joint_name in JOINTS],
                "values_rad": list(joint_state_rad),
                "q": list(joint_state_rad),
            },
            "tcp_pose": {
                "xyz": list(tcp_xyz),
                "rpy": list(tcp_rpy),
            },
            "gripper_state": to_jsonable(gripper_state)
            if gripper_state is not None
            else {
                "available": False,
                "open_ratio": None,
            },
            "raw_present_position": {joint_name: int(raw_present[joint_name]) for joint_name in JOINTS},
            "relative_raw_position": raw_relative,
            "startup_raw_position": startup_raw,
            "motor_position_deg": motor_deg,
            "output_position_deg": output_deg,
            "multi_turn_state": to_jsonable(self._multi_turn_state),
            "mode_by_joint": {joint_name: POSITION_MODE_VALUE for joint_name in JOINTS},
            "timestamp": time.time(),
        }
        return _as_attrdict(state)

    def get_state(self) -> AttrDict:
        bus = self._ensure_bus()
        raw_present = self._read_raw_present_position(bus)
        return self._build_state(raw_present)

    def read(self) -> AttrDict:
        return self.get_state()

    def _coerce_joint_targets_deg(self, targets: Mapping[str, Any] | Iterable[Any]) -> dict[str, float]:
        if isinstance(targets, Mapping):
            coerced: dict[str, float] = {}
            for joint_name, target_value in targets.items():
                joint_key = str(joint_name)
                if joint_key not in JOINTS:
                    raise ValidationError(f"Unknown joint: {joint_key}")
                coerced[joint_key] = float(target_value)
            return coerced

        values = list(targets)
        if len(values) != len(JOINTS):
            raise ValidationError(f"Expected {len(JOINTS)} joint values, got {len(values)}")
        return {joint_name: math.degrees(float(values[idx])) for idx, joint_name in enumerate(JOINTS)}

    def _hold_raw_positions(self, bus, raw_present: Mapping[str, int]) -> None:
        self._write_raw_goal_positions(bus, raw_present)

    def _write_raw_goal_positions(self, bus, goal_raw_by_joint: Mapping[str, int]) -> None:
        for joint_name in JOINTS:
            if joint_name not in goal_raw_by_joint:
                continue
            bus.write("Goal_Position", joint_name, int(goal_raw_by_joint[joint_name]), normalize=False)

    def _build_raw_hold_command(self, bus=None) -> dict[str, int]:
        # Compatibility helper for older skill scripts: when torque is re-enabled
        # we hold the exact current raw register values instead of reusing any
        # historical multi-turn state. This matches the rebuilt "startup is the
        # only reference" design and avoids bringing back the removed turn tracker.
        raw_present = self._read_raw_present_position(bus or self._ensure_bus())
        return {joint_name: int(raw_present[joint_name]) for joint_name in JOINTS if joint_name in raw_present}

    def _goal_error_raw(self, joint_name: str, present_raw: int | float, goal_raw: int | float) -> int:
        spec = self._joint_specs[joint_name]
        normalized_present = spec.policy.normalize_present_raw(present_raw)
        normalized_goal = spec.policy.normalize_present_raw(goal_raw)
        return int(spec.policy.relative_raw(current_raw=normalized_present, startup_raw=normalized_goal))

    def capture_hold_state(self, bus=None) -> dict[str, Any]:
        active_bus = bus or self._ensure_bus()
        payload: dict[str, Any] = {
            "joint_goal_raw": self._build_raw_hold_command(active_bus),
        }
        gripper_raw = self._read_gripper_register_raw(active_bus)
        if gripper_raw is not None:
            payload["gripper_goal_raw"] = int(gripper_raw)
        return payload

    def apply_hold_state(self, hold_state: Mapping[str, Any] | None, *, bus=None) -> dict[str, Any]:
        active_bus = bus or self._ensure_bus()
        payload = dict(hold_state or {})
        joint_goal_raw = payload.get("joint_goal_raw")
        if isinstance(joint_goal_raw, Mapping):
            self._write_raw_goal_positions(
                active_bus,
                {str(joint_name): int(value) for joint_name, value in joint_goal_raw.items() if joint_name in JOINTS},
            )

        gripper_goal_raw = payload.get("gripper_goal_raw")
        applied_gripper_raw: int | None = None
        if isinstance(gripper_goal_raw, (int, float)):
            applied_gripper_raw = int(gripper_goal_raw)
            self.write_gripper_raw(applied_gripper_raw, bus=active_bus)

        return {
            "joint_goal_raw": dict(payload.get("joint_goal_raw", {})) if isinstance(joint_goal_raw, Mapping) else {},
            "gripper_goal_raw": applied_gripper_raw,
        }

    def disable_torque(self) -> None:
        bus = self._ensure_bus()
        disable = getattr(bus, "disable_torque", None)
        if callable(disable):
            disable()

    def enable_torque(self) -> None:
        bus = self._ensure_bus()
        enable = getattr(bus, "enable_torque", None)
        if callable(enable):
            enable()

    def _wait_for_motion(self, bus, goal_raw_by_joint: Mapping[str, int], duration: float, timeout: float | None) -> dict[str, Any]:
        if not goal_raw_by_joint:
            return {
                "settled": True,
                "goal_raw_by_joint": {},
                "present_raw_by_joint": {},
                "error_by_joint": {},
                "moving_flags": None,
            }

        duration = max(0.0, float(duration))
        wait_window_s = (
            float(timeout)
            if timeout is not None
            else max(DEFAULT_JOINT_WAIT_TIMEOUT_S, duration + float(self.config.cartesian_settle_time_s) + 0.5)
        )
        deadline = time.monotonic() + wait_window_s
        if duration > 0.0:
            time.sleep(min(duration, 0.1))

        last_snapshot: dict[str, Any] = {
            "settled": False,
            "goal_raw_by_joint": {str(joint_name): int(goal_raw) for joint_name, goal_raw in goal_raw_by_joint.items()},
            "present_raw_by_joint": {},
            "error_by_joint": {},
            "moving_flags": None,
        }

        while time.monotonic() < deadline:
            try:
                raw_present = self._read_raw_present_position(bus)
            except Exception:
                last_snapshot["read_error"] = "present_position_read_failed"
                return last_snapshot

            error_by_joint = {
                joint_name: self._goal_error_raw(
                    joint_name,
                    present_raw=raw_present[joint_name],
                    goal_raw=goal_raw,
                )
                for joint_name, goal_raw in goal_raw_by_joint.items()
            }
            all_within_tolerance = all(
                abs(int(error_raw)) <= int(DEFAULT_JOINT_SETTLE_TOLERANCE_RAW)
                for error_raw in error_by_joint.values()
            )

            moving_flags: list[int] | None = None
            try:
                moving_flags = [
                    int(bus.read("Moving", joint_name, normalize=False))
                    for joint_name in goal_raw_by_joint
                ]
            except Exception:
                moving_flags = None

            last_snapshot = {
                "settled": False,
                "goal_raw_by_joint": {str(joint_name): int(goal_raw) for joint_name, goal_raw in goal_raw_by_joint.items()},
                "present_raw_by_joint": {str(joint_name): int(raw_present[joint_name]) for joint_name in goal_raw_by_joint},
                "error_by_joint": {str(joint_name): int(error_raw) for joint_name, error_raw in error_by_joint.items()},
                "moving_flags": list(moving_flags) if moving_flags is not None else None,
            }

            if all_within_tolerance and (moving_flags is None or not any(moving_flags)):
                last_snapshot["settled"] = True
                return last_snapshot
            time.sleep(DEFAULT_JOINT_POLL_INTERVAL_S)

        return last_snapshot

    def _interpolate_raw_goal_positions(
        self,
        *,
        bus,
        start_relative_raw_by_joint: Mapping[str, float],
        target_relative_raw_by_joint: Mapping[str, float],
        duration: float,
    ) -> bool:
        duration = max(0.0, float(duration))
        update_hz = max(1.0, float(self.config.joint_update_hz))
        step_count = int(round(duration * update_hz))
        if step_count <= 1:
            return False

        dt = duration / float(step_count)
        for step_index in range(1, step_count + 1):
            alpha = float(step_index) / float(step_count)
            step_goal_raw: dict[str, int] = {}
            for joint_name, target_relative_raw in target_relative_raw_by_joint.items():
                start_relative_raw = float(start_relative_raw_by_joint.get(joint_name, target_relative_raw))
                interpolated_relative_raw = start_relative_raw + (float(target_relative_raw) - start_relative_raw) * alpha
                step_goal_raw[joint_name] = self._relative_raw_to_absolute_goal_raw(joint_name, interpolated_relative_raw)
            self._write_raw_goal_positions(bus, step_goal_raw)
            if step_index < step_count:
                time.sleep(max(0.0, dt))
        return True

    def _append_sdk_debug_log(self, message: str) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.runtime_dir / "sdk_multi_turn_debug.log"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message.rstrip()}\n")

    def move_joints(
        self,
        targets_deg: Mapping[str, Any] | Iterable[Any],
        *,
        multi_turn_targets_continuous_raw: Mapping[str, Any] | None = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        bus = self._ensure_bus()
        raw_present_before = self._read_raw_present_position(bus)
        before_state = self._build_state(raw_present_before)
        target_deg_by_joint = self._coerce_joint_targets_deg(targets_deg)
        override_relative_raw = {
            str(joint_name): float(value) for joint_name, value in dict(multi_turn_targets_continuous_raw or {}).items()
        }
        invalid_override_joints = [joint_name for joint_name in override_relative_raw if joint_name not in MULTI_TURN_JOINTS]
        if invalid_override_joints:
            raise ValidationError(
                "Continuous-raw overrides are only valid for multi-turn joints: "
                + ", ".join(sorted(invalid_override_joints))
            )
        goal_raw_by_joint: dict[str, int] = {}
        accepted_target_deg: dict[str, float] = {}
        effective_relative_raw: dict[str, float] = {}
        start_relative_raw_by_joint = {
            joint_name: float(before_state["relative_raw_position"][joint_name])
            for joint_name in goal_raw_by_joint
        }

        for joint_name in sorted(set(target_deg_by_joint) | set(override_relative_raw)):
            if joint_name in override_relative_raw:
                relative_raw = float(override_relative_raw[joint_name])
                goal_raw = self._continuous_raw_to_multi_turn_goal_raw(joint_name, relative_raw)
                accepted_target_deg[joint_name] = float(self._relative_raw_to_joint_deg(joint_name, relative_raw))
                effective_relative_raw[joint_name] = relative_raw
            else:
                accepted_target_deg[joint_name] = float(target_deg_by_joint[joint_name])
                relative_raw = float(self._joint_deg_to_relative_raw(joint_name, accepted_target_deg[joint_name]))
                goal_raw = self._joint_deg_to_absolute_goal_raw(joint_name, accepted_target_deg[joint_name])
                effective_relative_raw[joint_name] = relative_raw
            goal_raw_by_joint[joint_name] = int(goal_raw)
            start_relative_raw_by_joint[joint_name] = float(before_state["relative_raw_position"][joint_name])

        if trace:
            self._append_sdk_debug_log(
                "[INFO] move_goal start "
                f"target_joint_deg={json.dumps(accepted_target_deg, ensure_ascii=False, sort_keys=True)} "
                f"target_multi_turn_continuous_raw={json.dumps({k: v for k, v in effective_relative_raw.items() if k in MULTI_TURN_JOINTS}, ensure_ascii=False, sort_keys=True)} "
                f"before_joint_deg={json.dumps({joint_name: before_state['joint_state'][joint_name] for joint_name in accepted_target_deg}, ensure_ascii=False, sort_keys=True)} "
                f"bus_cmd={json.dumps(goal_raw_by_joint, ensure_ascii=False, sort_keys=True)}"
            )

        interpolated = False
        if wait and goal_raw_by_joint:
            interpolated = self._interpolate_raw_goal_positions(
                bus=bus,
                start_relative_raw_by_joint=start_relative_raw_by_joint,
                target_relative_raw_by_joint=effective_relative_raw,
                duration=float(duration),
            )
        if not interpolated:
            self._write_raw_goal_positions(bus, goal_raw_by_joint)

        for joint_name, goal_raw in goal_raw_by_joint.items():
            if joint_name in MULTI_TURN_JOINTS:
                self._last_multi_turn_goal_raw_mod[joint_name] = int(goal_raw)

        if wait:
            wait_summary = self._wait_for_motion(bus, goal_raw_by_joint, duration=float(duration), timeout=timeout)
            if not bool(wait_summary.get("settled", False)):
                error_by_joint = dict(wait_summary.get("error_by_joint", {}))
                present_raw_by_joint = dict(wait_summary.get("present_raw_by_joint", {}))
                lines: list[str] = []
                for joint_name, error_raw in sorted(
                    error_by_joint.items(),
                    key=lambda item: abs(int(item[1])),
                    reverse=True,
                ):
                    lines.append(
                        f"{joint_name}: present_raw={present_raw_by_joint.get(joint_name)}, "
                        f"goal_raw={goal_raw_by_joint.get(joint_name)}, "
                        f"error_raw={int(error_raw)}, "
                        f"error_joint_deg={float(self._relative_raw_to_joint_deg(joint_name, int(error_raw))):.2f}"
                    )
                if not lines and wait_summary.get("read_error"):
                    lines.append(str(wait_summary["read_error"]))
                raise HardwareError(
                    "Joint motion did not settle before timeout:\n" + "\n".join(lines or ["unknown motion wait failure"])
                )

        state = self.get_state()
        result = {
            "action": "move_joints",
            "targets_deg": accepted_target_deg,
            "goal_raw": goal_raw_by_joint,
            "state": state,
        }

        if trace:
            after_multi_turn = {joint_name: state["joint_state"][joint_name] for joint_name in MULTI_TURN_JOINTS}
            self._append_sdk_debug_log(
                "[INFO] move_goal end "
                f"after_joint_deg={json.dumps({joint_name: state['joint_state'][joint_name] for joint_name in accepted_target_deg}, ensure_ascii=False, sort_keys=True)} "
                f"joint_error_deg={json.dumps({joint_name: float(state['joint_state'][joint_name]) - float(accepted_target_deg[joint_name]) for joint_name in accepted_target_deg}, ensure_ascii=False, sort_keys=True)} "
                f"multi_turn_state={json.dumps(after_multi_turn, ensure_ascii=False, sort_keys=True)} "
                "correction_count=0"
            )

        return result

    def move_joint(
        self,
        *,
        joint: str,
        target_deg: float | None = None,
        delta_deg: float | None = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        joint_name = str(joint).strip()
        if joint_name not in JOINTS:
            raise ValidationError(f"Unknown joint: {joint_name}")
        if (target_deg is None) == (delta_deg is None):
            raise ValidationError("Exactly one of target_deg or delta_deg must be provided")

        current_state = self.get_state()
        base_deg = float(current_state["joint_state"][joint_name])
        final_target_deg = float(target_deg) if target_deg is not None else base_deg + float(delta_deg)
        move_result = self.move_joints(
            {joint_name: final_target_deg},
            duration=float(duration),
            wait=bool(wait),
            timeout=timeout,
            trace=trace,
        )
        move_result["target_deg"] = float(final_target_deg)
        return move_result

    def home(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        # Startup position is intentionally the runtime zero reference, so "home"
        # simply commands zero relative joint angle for every axis.
        result = self.move_joints(
            {joint_name: 0.0 for joint_name in JOINTS},
            duration=float(duration),
            wait=bool(wait),
            timeout=timeout,
        )
        result["action"] = "home"
        return result

    def stop(self) -> dict[str, Any]:
        bus = self._ensure_bus()
        raw_present = self._read_raw_present_position(bus)
        self.apply_hold_state(self.capture_hold_state(bus), bus=bus)
        return {
            "action": "stop",
            "state": self._build_state(raw_present),
        }

    def _resolve_ik_seed_q(self, *, q0: Iterable[Any] | None, seed_policy: str) -> list[float]:
        if q0 is not None:
            payload = list(q0)
            if len(payload) != len(JOINTS):
                raise ValidationError(f"q0 must contain {len(JOINTS)} joint values, got {len(payload)}")
            return [float(value) for value in payload]

        policy = str(seed_policy or "current").strip().lower()
        if policy in {"current", "now", ""}:
            state = self.get_state()
            return [float(value) for value in state["joint_state"]["q"]]
        if policy in {"home", "startup", "zero"}:
            return [0.0 for _ in JOINTS]
        raise ValidationError(f"Unsupported IK seed_policy: {seed_policy}")

    def move_pose(
        self,
        xyz: Iterable[Any],
        rpy: Iterable[Any] | None = None,
        *,
        q0: Iterable[Any] | None = None,
        seed_policy: str = "current",
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        target_xyz = _coerce_vector3(xyz, name="xyz")
        target_rpy = _coerce_vector3(rpy, name="rpy") if rpy is not None else None
        seed_q = self._resolve_ik_seed_q(q0=q0, seed_policy=seed_policy)
        kinematics = self._ensure_kinematics(required=True)
        assert kinematics is not None

        ik_result = kinematics.inverse(
            target_xyz=target_xyz,
            target_rpy=target_rpy,
            seed_q_user=seed_q,
            max_iters=int(self.config.ik_max_iters),
            residual_threshold=max(1e-6, float(self.config.ik_target_tol_m) * 0.5),
        )

        if trace:
            self._append_sdk_debug_log(
                "[IK] move_pose "
                f"target_xyz_m={json.dumps([float(value) for value in target_xyz], ensure_ascii=False)} "
                f"target_rpy_rad={json.dumps([float(value) for value in target_rpy], ensure_ascii=False) if target_rpy is not None else 'null'} "
                f"seed_q_rad={json.dumps([float(value) for value in seed_q], ensure_ascii=False)} "
                f"solution_q_rad={json.dumps([float(value) for value in ik_result.q_user.tolist()], ensure_ascii=False)} "
                f"predicted_xyz_m={json.dumps([float(value) for value in ik_result.xyz.tolist()], ensure_ascii=False)} "
                f"predicted_rpy_rad={json.dumps([float(value) for value in ik_result.rpy.tolist()], ensure_ascii=False)} "
                f"pos_error_m={float(ik_result.pos_error_m):.6f} "
                f"rot_error_rad={'null' if ik_result.rot_error_rad is None else f'{float(ik_result.rot_error_rad):.6f}'}"
            )

        if ik_result.pos_error_m > float(self.config.max_ee_pos_err_m):
            raise IKError(
                "IK solution position error exceeds the configured limit: "
                f"{ik_result.pos_error_m:.4f} m > {float(self.config.max_ee_pos_err_m):.4f} m"
            )
        if (
            target_rpy is not None
            and ik_result.rot_error_rad is not None
            and ik_result.rot_error_rad > float(self.config.ik_orientation_tol_rad)
        ):
            raise IKError(
                "IK solution orientation error exceeds the configured limit: "
                f"{ik_result.rot_error_rad:.4f} rad > {float(self.config.ik_orientation_tol_rad):.4f} rad"
            )

        move_result = self.move_joints(
            ik_result.q_user.tolist(),
            duration=float(duration),
            wait=bool(wait),
            timeout=timeout,
            trace=trace,
        )
        move_result["action"] = "move_pose"
        move_result["target_xyz_m"] = [float(value) for value in target_xyz]
        move_result["target_rpy_rad"] = [float(value) for value in target_rpy] if target_rpy is not None else None
        move_result["orientation_mode"] = "constrained" if target_rpy is not None else "position_only"
        move_result["ik"] = {
            "seed_q_rad": [float(value) for value in seed_q],
            "solution_q_rad": [float(value) for value in ik_result.q_user.tolist()],
            "predicted_xyz_m": [float(value) for value in ik_result.xyz.tolist()],
            "predicted_rpy_rad": [float(value) for value in ik_result.rpy.tolist()],
            "position_error_m": float(ik_result.pos_error_m),
            "orientation_error_rad": (
                float(ik_result.rot_error_rad) if ik_result.rot_error_rad is not None else None
            ),
            "backend": "pybullet",
        }
        return move_result

    def move_to(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.move_pose(*args, **kwargs)

    def move_tcp(
        self,
        x: float,
        y: float,
        z: float,
        rpy: Iterable[Any] | None = None,
        *,
        frame: str = "base",
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        frame_norm = "tool" if str(frame or "").strip().lower() == "tool" else "base"
        if frame_norm == "tool":
            delta_rpy = _coerce_vector3(rpy, name="rpy") if rpy is not None else [0.0, 0.0, 0.0]
            return self.move_delta(
                dx=float(x),
                dy=float(y),
                dz=float(z),
                drx=float(delta_rpy[0]),
                dry=float(delta_rpy[1]),
                drz=float(delta_rpy[2]),
                frame="tool",
                duration=float(duration),
                wait=bool(wait),
                timeout=timeout,
                trace=trace,
            )

        return self.move_pose(
            xyz=[float(x), float(y), float(z)],
            rpy=rpy,
            duration=float(duration),
            wait=bool(wait),
            timeout=timeout,
            trace=trace,
        )

    def move_delta(
        self,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        drx: float = 0.0,
        dry: float = 0.0,
        drz: float = 0.0,
        frame: str = "base",
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        if (
            abs(float(dx)) <= 1e-12
            and abs(float(dy)) <= 1e-12
            and abs(float(dz)) <= 1e-12
            and abs(float(drx)) <= 1e-12
            and abs(float(dry)) <= 1e-12
            and abs(float(drz)) <= 1e-12
        ):
            return {
                "action": "move_delta",
                "frame": str(frame),
                "delta": {
                    "dx": float(dx),
                    "dy": float(dy),
                    "dz": float(dz),
                    "drx": float(drx),
                    "dry": float(dry),
                    "drz": float(drz),
                },
                "state": self.get_state(),
            }
        kinematics = self._ensure_kinematics(required=True)
        assert kinematics is not None
        current_pose = self.get_end_effector_pose()
        target_xyz, target_rpy = kinematics.compose_delta_target(
            current_xyz=current_pose["xyz"],
            current_rpy=current_pose["rpy"],
            delta_xyz=[float(dx), float(dy), float(dz)],
            delta_rpy=[float(drx), float(dry), float(drz)],
            frame=str(frame),
        )
        result = self.move_pose(
            xyz=target_xyz.tolist(),
            rpy=target_rpy.tolist(),
            duration=float(duration),
            wait=bool(wait),
            timeout=timeout,
            trace=trace,
        )
        result["action"] = "move_delta"
        result["frame"] = "tool" if str(frame or "").strip().lower() == "tool" else "base"
        result["delta"] = {
            "dx": float(dx),
            "dy": float(dy),
            "dz": float(dz),
            "drx": float(drx),
            "dry": float(dry),
            "drz": float(drz),
        }
        result["target_xyz_m"] = [float(value) for value in target_xyz.tolist()]
        result["target_rpy_rad"] = [float(value) for value in target_rpy.tolist()]
        return result

    def write_gripper_raw(self, goal_raw: int | None, *, bus=None) -> bool:
        if goal_raw is None:
            return False
        spec = self._require_gripper_spec()
        active_bus = bus or self._ensure_bus()
        active_bus.write("Goal_Position", spec.name, int(goal_raw), normalize=False)
        self._last_gripper_goal_raw = int(goal_raw)
        return True

    def _wait_for_gripper_settled(
        self,
        *,
        goal_raw: int,
        timeout: float | None,
        settle_tolerance_raw: int = DEFAULT_GRIPPER_SETTLE_TOLERANCE_RAW,
        poll_interval_s: float = DEFAULT_GRIPPER_POLL_INTERVAL_S,
    ) -> dict[str, Any]:
        spec = self._require_gripper_spec()
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        goal_adjusted_raw = self._gripper_register_raw_to_adjusted_raw(goal_raw)

        while True:
            state = self.get_gripper_state()
            if state is None:
                raise CapabilityError("Optional gripper disappeared while waiting for motion completion.")
            present_adjusted_raw = int(state["adjusted_raw"])
            error_raw = _signed_single_turn_delta(goal_adjusted_raw, present_adjusted_raw)
            moving = int(self._ensure_bus().read("Moving", spec.name, normalize=False))
            velocity = float(self._ensure_bus().read("Present_Velocity", spec.name, normalize=False))
            current = float(self._ensure_bus().read("Present_Current", spec.name, normalize=False))
            if abs(int(error_raw)) <= int(settle_tolerance_raw):
                return {
                    "settled": True,
                    "present_raw": int(state["present_raw"]),
                    "present_adjusted_raw": int(present_adjusted_raw),
                    "error_raw": int(error_raw),
                    "moving": moving,
                    "velocity": velocity,
                    "current": current,
                }
            if deadline is not None and time.monotonic() > deadline:
                return {
                    "settled": False,
                    "present_raw": int(state["present_raw"]),
                    "present_adjusted_raw": int(present_adjusted_raw),
                    "error_raw": int(error_raw),
                    "moving": moving,
                    "velocity": velocity,
                    "current": current,
                }
            time.sleep(max(0.005, float(poll_interval_s)))

    def set_gripper(
        self,
        *,
        open_ratio: float = 1.0,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        goal_raw = self._open_ratio_to_gripper_goal_raw(open_ratio)
        self.write_gripper_raw(goal_raw)
        settled = None
        if wait:
            effective_timeout = timeout if timeout is not None else max(float(duration) * 2.0, 0.5)
            settled = self._wait_for_gripper_settled(goal_raw=goal_raw, timeout=effective_timeout)
        state = self.get_state()
        return {
            "action": "set_gripper",
            "goal_open_ratio": float(min(1.0, max(0.0, float(open_ratio)))),
            "goal_raw": int(goal_raw),
            "wait": bool(wait),
            "settled": settled,
            "state": state,
        }

    def open_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self.set_gripper(open_ratio=1.0, duration=duration, wait=wait, timeout=timeout)

    def close_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self.set_gripper(open_ratio=0.0, duration=duration, wait=wait, timeout=timeout)

    def meta(self) -> AttrDict:
        startup_raw = {
            joint_name: state.startup_raw
            for joint_name, state in self._joint_runtime_state.items()
        }
        return _as_attrdict(
            {
                "joint_limits_deg": {},
                "joint_scales": dict(self.config.joint_scales),
                "model_offsets_deg": dict(self.config.model_offsets_deg),
                "joint_name_aliases": dict(self.config.joint_name_aliases),
                "bounded_single_turn_joints": list(BOUNDED_SINGLE_TURN_JOINTS),
                "multi_turn_joints": list(MULTI_TURN_JOINTS),
                "startup_raw_position": startup_raw,
                "gripper": to_jsonable(self._build_gripper_state()),
                "kinematics": {
                    "available": bool(self._ensure_kinematics(required=False) is not None),
                    "backend": "pybullet" if PYBULLET_AVAILABLE else None,
                    "target_frame": str(self.config.target_frame),
                    "error": self._kinematics_error_text or None,
                },
                "config": {
                    "port": self.config.port,
                    "robot_id": self.config.robot_id,
                    "calibration_path": self.config.calibration_path,
                    "urdf_path": self.config.urdf_path,
                },
            }
        )


Robot = SoArmMoceController


__all__ = [
    "BOUNDED_SINGLE_TURN_JOINTS",
    "CapabilityError",
    "DEFAULT_JOINT_NAME_ALIASES",
    "DEFAULT_MODEL_OFFSETS_DEG",
    "HardwareError",
    "IKError",
    "JOINTS",
    "MULTI_TURN_ABSOLUTE_RAW_LIMIT",
    "MULTI_TURN_DISABLED_LIMIT_RAW",
    "MULTI_TURN_JOINTS",
    "MULTI_TURN_PHASE_VALUE",
    "POSITION_MODE_VALUE",
    "Robot",
    "SKILL_ROOT",
    "SoArmMoceConfig",
    "SoArmMoceController",
    "ValidationError",
    "resolve_config",
]
