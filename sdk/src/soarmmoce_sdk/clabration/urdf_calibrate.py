#!/usr/bin/env python3
"""Calibrate bounded single-turn joints and normalize absolute-raw joint config."""

import json
import math
import signal
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

SDK_SRC = Path(__file__).resolve().parents[2]
sdk_src_str = str(SDK_SRC)
if sdk_src_str not in sys.path:
    sys.path.insert(0, sdk_src_str)

try:
    import draccus
    DRACCUS_AVAILABLE = True
except ImportError:  # pragma: no cover - lightweight import fallback for tests
    draccus = None
    DRACCUS_AVAILABLE = False

from soarmmoce_sdk.cli_common import run_and_print
from soarmmoce_sdk import (
    BOUNDED_SINGLE_TURN_JOINTS,
    JOINTS,
    MULTI_TURN_DISABLED_LIMIT_RAW,
    MULTI_TURN_JOINTS,
    MULTI_TURN_PHASE_VALUE,
    POSITION_MODE_VALUE,
    resolve_config,
)


SUPPORTED_ROBOT_TYPES = {"soarmmoce"}
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_MOTOR_MODEL = "sts3215"
RAW_COUNTS_PER_REV = 4096
SINGLE_TURN_ZERO_RAW = RAW_COUNTS_PER_REV // 2
SINGLE_TURN_ZERO_TOLERANCE_RAW = 16

ARM_MOTOR_IDS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
}
GRIPPER_JOINT_NAME = "gripper"
DEFAULT_GRIPPER_MOTOR_ID = 6
GRIPPER_REQUIRED_FIELDS = ("id", "homing_offset", "range_min", "range_max")

SDK_TO_URDF_JOINT = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}


class _CalibrationConnectTimeout(RuntimeError):
    pass


@dataclass
class CalibrateRobotConfig:
    type: str = "soarmmoce"
    port: str = ""
    id: str = ""
    calib_dir: str = ""
    output: str = ""
    motor_model: str = DEFAULT_MOTOR_MODEL
    apply_registers: bool = True
    save_json: bool = True
    display_values: bool = True
    prompt_existing: bool = False
    update_urdf_limits: bool = True
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S

    def __post_init__(self) -> None:
        robot_type = str(self.type or "").strip().lower()
        if robot_type not in SUPPORTED_ROBOT_TYPES:
            supported = ", ".join(sorted(SUPPORTED_ROBOT_TYPES))
            raise ValueError(f"Unsupported robot.type={self.type!r}. Expected one of: {supported}")


@dataclass
class CalibrateConfig:
    robot: CalibrateRobotConfig = field(default_factory=CalibrateRobotConfig)


def _wrap_position_raw(raw_value: int | float) -> int:
    return int(round(float(raw_value))) % RAW_COUNTS_PER_REV


def _single_turn_zero_present_raw(max_res: int) -> int:
    return min(int(max_res), int(SINGLE_TURN_ZERO_RAW))


def _read_json(path: str | Path) -> dict[str, Any]:
    payload_path = Path(path).expanduser().resolve()
    if not payload_path.exists():
        return {}
    loaded = json.loads(payload_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return loaded
    return {}


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    payload_path = Path(path).expanduser().resolve()
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_joint_snapshot(bus, joint: str, tracker=None, homing_offset_raw: int = 0) -> Dict[str, Any]:
    del tracker  # Compatibility placeholder for older call sites.
    register_raw = int(bus.read("Present_Position", joint, normalize=False))
    if joint in MULTI_TURN_JOINTS:
        position_key = int(register_raw) + int(homing_offset_raw)
        wrapped_raw = _wrap_position_raw(position_key)
    else:
        wrapped_raw = _wrap_position_raw(int(register_raw) + int(homing_offset_raw))
        position_key = int(wrapped_raw)
    return {
        "position": int(position_key),
        "position_wrapped": int(wrapped_raw),
        "position_register_raw": int(register_raw),
        "velocity": float(bus.read("Present_Velocity", joint, normalize=False)),
        "moving": int(bus.read("Moving", joint, normalize=False)),
        "current": float(bus.read("Present_Current", joint, normalize=False)),
    }


def _command_goal_from_reference(
    bus,
    joint: str,
    direction: int,
    step_raw: int,
    reference_position_raw: int,
) -> Dict[str, Any]:
    signed_direction = 1 if int(direction) >= 0 else -1
    goal_value = int(reference_position_raw) + signed_direction * max(1, int(step_raw))
    if joint not in MULTI_TURN_JOINTS:
        goal_value = min(RAW_COUNTS_PER_REV - 1, max(0, goal_value))
    bus.write("Goal_Position", joint, int(goal_value), normalize=False)
    return {
        "kind": "absolute_goal",
        "goal_value": int(goal_value),
        "from_position": int(reference_position_raw),
    }


def _build_single_turn_calibration_entry(
    *,
    current_cal,
    max_res: int,
    homing_offset_raw: int,
    zero_present_raw: int,
    min_present_raw: int,
    max_present_raw: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    range_min = min(int(min_present_raw), int(max_present_raw))
    range_max = max(int(min_present_raw), int(max_present_raw))
    range_min = max(0, min(int(max_res), range_min))
    range_max = max(0, min(int(max_res), range_max))
    if range_max <= range_min:
        raise ValueError(
            "Single-turn calibration produced an invalid range: "
            f"range_min={range_min}, range_max={range_max}"
        )
    if not (range_min <= int(zero_present_raw) <= range_max):
        raise ValueError(
            "Single-turn calibration range does not contain the calibrated zero point: "
            f"zero_present_raw={zero_present_raw}, range_min={range_min}, range_max={range_max}. "
            "Recalibrate without crossing the 0/4095 seam."
        )
    if (range_max - range_min) > int(max_res * 0.75):
        raise ValueError(
            "Single-turn calibration range is unexpectedly wide and likely crossed the 0/4095 seam: "
            f"range_min={range_min}, range_max={range_max}, max_res={max_res}. "
            "Choose a q=0 pose away from the seam and record the range again."
        )

    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(getattr(current_cal, "drive_mode", 0)),
        "homing_offset": int(homing_offset_raw),
        "range_min": int(range_min),
        "range_max": int(range_max),
        "zero_present_raw": int(zero_present_raw),
        "operating_mode": POSITION_MODE_VALUE,
    }
    result_payload = {
        "calibration_mode": "single_turn_current_pose_to_midpoint",
        "homing_offset": int(homing_offset_raw),
        "range_min": int(range_min),
        "range_max": int(range_max),
        "zero_present_raw": int(zero_present_raw),
        "target_zero_present_raw": int(_single_turn_zero_present_raw(max_res)),
    }
    return entry, result_payload


def _build_multi_turn_calibration_entry(
    *,
    current_cal,
    home_present_raw: int,
    home_present_wrapped_raw: int,
    min_present_raw: int,
    max_present_raw: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(getattr(current_cal, "drive_mode", 0)),
        "homing_offset": 0,
        "phase": MULTI_TURN_PHASE_VALUE,
        "range_min": MULTI_TURN_DISABLED_LIMIT_RAW,
        "range_max": MULTI_TURN_DISABLED_LIMIT_RAW,
        "operating_mode": POSITION_MODE_VALUE,
        "home_present_raw": int(home_present_raw),
        "home_present_wrapped_raw": int(home_present_wrapped_raw),
    }
    payload = {
        "calibration_mode": "multi_turn_mode0_absolute_position",
        "home_present_raw": int(home_present_raw),
        "home_present_wrapped_raw": int(home_present_wrapped_raw),
        "min_present_raw": int(min_present_raw),
        "max_present_raw": int(max_present_raw),
        "phase": MULTI_TURN_PHASE_VALUE,
        "operating_mode": POSITION_MODE_VALUE,
    }
    return entry, payload


def _disconnect_bus(bus) -> None:
    if bus is None:
        return
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def _connect_bus_with_timeout(bus, *, timeout_s: float, action: str) -> None:
    timeout_s = float(timeout_s)
    previous_handler = None

    if timeout_s > 0.0:
        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _CalibrationConnectTimeout(f"Timed out after {timeout_s:.1f}s while {action}")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)

    try:
        bus.connect()
    except _CalibrationConnectTimeout as exc:
        _disconnect_bus(bus)
        raise RuntimeError(
            f"{exc}. The script is hanging during serial handshake; check SOARMMOCE_PORT, power, and port occupancy."
        ) from exc
    except Exception:
        _disconnect_bus(bus)
        raise
    finally:
        if timeout_s > 0.0:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)


def _probe_optional_gripper(
    *,
    port: str,
    motor_model: str,
    motor_id: int,
    timeout_s: float,
    Motor,
    MotorNormMode,
    FeetechMotorsBus,
) -> bool:
    probe_bus = None
    try:
        probe_bus = FeetechMotorsBus(
            port=port,
            motors={
                GRIPPER_JOINT_NAME: Motor(
                    int(motor_id),
                    str(motor_model),
                    MotorNormMode.DEGREES,
                )
            },
        )
        _connect_bus_with_timeout(
            probe_bus,
            timeout_s=float(timeout_s),
            action=f"probing optional gripper motor id {int(motor_id)}",
        )
        probe_bus.read("Present_Position", GRIPPER_JOINT_NAME, normalize=False)
        return True
    except Exception as exc:
        print(
            f"[calibrate] Optional gripper motor id {int(motor_id)} was not detected; "
            f"continuing with arm joints only. ({exc.__class__.__name__})"
        )
        return False
    finally:
        _disconnect_bus(probe_bus)


def _connect_calibration_bus(
    *,
    port: str,
    motor_model: str,
    timeout_s: float,
    include_gripper: bool,
    gripper_motor_id: int,
):
    try:
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "Calibration requires the optional 'lerobot' dependency in the current Python environment."
        ) from exc

    motors = {
        joint_name: Motor(ARM_MOTOR_IDS[joint_name], str(motor_model), MotorNormMode.DEGREES)
        for joint_name in JOINTS
    }

    gripper_detected = False
    if bool(include_gripper):
        gripper_detected = _probe_optional_gripper(
            port=port,
            motor_model=motor_model,
            motor_id=int(gripper_motor_id),
            timeout_s=timeout_s,
            Motor=Motor,
            MotorNormMode=MotorNormMode,
            FeetechMotorsBus=FeetechMotorsBus,
        )
        if gripper_detected:
            motors[GRIPPER_JOINT_NAME] = Motor(
                int(gripper_motor_id),
                str(motor_model),
                MotorNormMode.DEGREES,
            )

    bus = FeetechMotorsBus(port=port, motors=motors)
    _connect_bus_with_timeout(bus, timeout_s=float(timeout_s), action="connecting to arm calibration bus")
    bus.disable_torque()
    if gripper_detected:
        print(f"[calibrate] Optional gripper detected on motor id {int(gripper_motor_id)}; including it in calibration.")
    return bus


def _resolve_paths(robot_cfg: CalibrateRobotConfig) -> Dict[str, Any]:
    base = resolve_config()
    target_port = str(robot_cfg.port or base.port).strip()
    target_robot_id = str(robot_cfg.id or base.robot_id).strip()
    if not target_robot_id:
        raise ValueError("robot.id must not be empty")

    if robot_cfg.calib_dir:
        target_calib_dir = Path(robot_cfg.calib_dir).expanduser().resolve()
    else:
        target_calib_dir = Path(base.calib_dir).resolve()

    target_source_path = (target_calib_dir / f"{target_robot_id}.json").resolve()
    if robot_cfg.output:
        output_path = Path(robot_cfg.output).expanduser().resolve()
    else:
        output_path = target_source_path

    return {
        "port": target_port,
        "robot_id": target_robot_id,
        "target_source_path": target_source_path,
        "output_path": output_path,
        "urdf_path": Path(base.urdf_path).resolve(),
        "joint_scales": dict(base.joint_scales),
        "gripper_available": bool(base.gripper_available),
    }


def _apply_single_turn_calibration(bus, joint_name: str, entry: Mapping[str, Any]) -> None:
    bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)
    bus.write("Homing_Offset", joint_name, int(entry["homing_offset"]), normalize=False)
    bus.write("Min_Position_Limit", joint_name, int(entry["range_min"]), normalize=False)
    bus.write("Max_Position_Limit", joint_name, int(entry["range_max"]), normalize=False)


def _bus_has_gripper(bus) -> bool:
    motors = getattr(bus, "motors", {})
    return isinstance(motors, Mapping) and GRIPPER_JOINT_NAME in motors


def _gripper_motor_id_from_payload(payload: Mapping[str, Any]) -> int:
    entry = payload.get(GRIPPER_JOINT_NAME)
    if isinstance(entry, Mapping):
        try:
            return int(entry.get("id", DEFAULT_GRIPPER_MOTOR_ID))
        except Exception:
            return DEFAULT_GRIPPER_MOTOR_ID
    return DEFAULT_GRIPPER_MOTOR_ID


def _complete_gripper_seed_entry(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = payload.get(GRIPPER_JOINT_NAME)
    if not isinstance(entry, Mapping):
        return None
    if any(field_name not in entry for field_name in GRIPPER_REQUIRED_FIELDS):
        return None
    result: dict[str, Any] = {
        "id": int(entry["id"]),
        "drive_mode": int(entry.get("drive_mode", 0)),
        "homing_offset": int(entry["homing_offset"]),
        "range_min": int(entry["range_min"]),
        "range_max": int(entry["range_max"]),
    }
    if "operating_mode" in entry:
        result["operating_mode"] = int(entry["operating_mode"])
    if "zero_present_raw" in entry:
        result["zero_present_raw"] = int(entry["zero_present_raw"])
    return result


def _build_gripper_calibration_entry(
    *,
    current_hw_calib: Mapping[str, Any],
    seed_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    seed_entry = _complete_gripper_seed_entry(seed_payload)
    if seed_entry is not None:
        result_payload = {
            "calibration_mode": "reuse_existing_gripper",
            "id": int(seed_entry["id"]),
            "homing_offset": int(seed_entry["homing_offset"]),
            "range_min": int(seed_entry["range_min"]),
            "range_max": int(seed_entry["range_max"]),
        }
        if "zero_present_raw" in seed_entry:
            result_payload["zero_present_raw"] = int(seed_entry["zero_present_raw"])
        return seed_entry, result_payload

    current_cal = current_hw_calib.get(GRIPPER_JOINT_NAME)
    if current_cal is None:
        raise KeyError(f"Optional gripper was detected but no hardware calibration was read for {GRIPPER_JOINT_NAME!r}")

    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(getattr(current_cal, "drive_mode", 0)),
        "homing_offset": int(getattr(current_cal, "homing_offset", 0)),
        "range_min": int(getattr(current_cal, "range_min", 0)),
        "range_max": int(getattr(current_cal, "range_max", RAW_COUNTS_PER_REV - 1)),
    }
    result_payload = {
        "calibration_mode": "read_existing_hardware_gripper",
        "id": int(entry["id"]),
        "homing_offset": int(entry["homing_offset"]),
        "range_min": int(entry["range_min"]),
        "range_max": int(entry["range_max"]),
    }
    return entry, result_payload


def _apply_recorded_gripper_range(
    entry: Mapping[str, Any],
    *,
    min_present_raw: int,
    max_present_raw: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    homing_offset = int(entry.get("homing_offset", 0))
    adjusted_values = sorted(
        [
            _wrap_position_raw(int(min_present_raw) + homing_offset),
            _wrap_position_raw(int(max_present_raw) + homing_offset),
        ]
    )
    updated_entry = dict(entry)
    updated_entry["range_min"] = int(adjusted_values[0])
    updated_entry["range_max"] = int(adjusted_values[1])
    return updated_entry, {
        "range_capture": "recorded",
        "recorded_min_present_raw": int(min_present_raw),
        "recorded_max_present_raw": int(max_present_raw),
        "range_min": int(updated_entry["range_min"]),
        "range_max": int(updated_entry["range_max"]),
        "homing_offset": int(homing_offset),
    }


def _apply_gripper_calibration(bus, entry: Mapping[str, Any]) -> None:
    bus.write("Operating_Mode", GRIPPER_JOINT_NAME, POSITION_MODE_VALUE, normalize=False)
    bus.write("Homing_Offset", GRIPPER_JOINT_NAME, int(entry["homing_offset"]), normalize=False)
    bus.write("Min_Position_Limit", GRIPPER_JOINT_NAME, int(entry["range_min"]), normalize=False)
    bus.write("Max_Position_Limit", GRIPPER_JOINT_NAME, int(entry["range_max"]), normalize=False)


def _single_turn_entry_to_limit_rad(
    *,
    joint_name: str,
    entry: Mapping[str, Any],
    joint_scales: Mapping[str, Any],
) -> tuple[float, float]:
    zero_raw = int(entry.get("zero_present_raw", SINGLE_TURN_ZERO_RAW))
    reduction_ratio = float(joint_scales[joint_name])
    if abs(reduction_ratio) < 1e-9:
        raise ValueError(f"Joint {joint_name} has invalid joint scale 0")

    values_rad = []
    for raw_value in (int(entry["range_min"]), int(entry["range_max"])):
        relative_raw = int(raw_value) - int(zero_raw)
        motor_deg = float(relative_raw) * 360.0 / float(RAW_COUNTS_PER_REV)
        joint_deg = motor_deg / reduction_ratio
        values_rad.append(math.radians(joint_deg))
    return float(min(values_rad)), float(max(values_rad))


def _write_bounded_single_turn_limits_to_urdf(
    *,
    urdf_path: Path,
    bounded_entries: Mapping[str, Mapping[str, Any]],
    joint_scales: Mapping[str, Any],
) -> dict[str, Any]:
    if not urdf_path.exists():
        raise FileNotFoundError(f"URDF not found: {urdf_path}")

    tree = ET.parse(urdf_path)
    root = tree.getroot()
    written: dict[str, dict[str, float]] = {}
    for joint_name, entry in bounded_entries.items():
        if joint_name not in SDK_TO_URDF_JOINT:
            continue
        lower_rad, upper_rad = _single_turn_entry_to_limit_rad(
            joint_name=joint_name,
            entry=entry,
            joint_scales=joint_scales,
        )
        urdf_joint = SDK_TO_URDF_JOINT[joint_name]
        joint_elem = root.find(f"./joint[@name='{urdf_joint}']")
        if joint_elem is None:
            raise KeyError(f"Joint {urdf_joint!r} not found in {urdf_path}")
        limit_elem = joint_elem.find("limit")
        if limit_elem is None:
            limit_elem = ET.SubElement(joint_elem, "limit")
        limit_elem.set("lower", f"{lower_rad:.8f}")
        limit_elem.set("upper", f"{upper_rad:.8f}")
        written[joint_name] = {
            "lower_rad": float(lower_rad),
            "upper_rad": float(upper_rad),
            "lower_deg": float(math.degrees(lower_rad)),
            "upper_deg": float(math.degrees(upper_rad)),
        }

    if not written:
        return {"updated": False, "written_limits": {}}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = urdf_path.with_name(f"{urdf_path.name}.bak_{stamp}")
    shutil.copy2(urdf_path, backup_path)
    tree.write(urdf_path, encoding="unicode", xml_declaration=True)
    return {
        "updated": True,
        "urdf_path": str(urdf_path),
        "backup_path": str(backup_path),
        "written_limits": written,
    }


def _apply_multi_turn_fixed_config(bus, joint_name: str, entry: Mapping[str, Any]) -> None:
    bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)
    bus.write("Homing_Offset", joint_name, 0, normalize=False)
    bus.write("Phase", joint_name, int(entry["phase"]), normalize=False)
    bus.write("Min_Position_Limit", joint_name, MULTI_TURN_DISABLED_LIMIT_RAW, normalize=False)
    bus.write("Max_Position_Limit", joint_name, MULTI_TURN_DISABLED_LIMIT_RAW, normalize=False)


def _read_homing_offset(bus, joint_name: str) -> int:
    try:
        return int(bus.read("Homing_Offset", joint_name, normalize=False))
    except Exception:
        return 0


def _set_homing_offset_for_current_pose(
    bus,
    joint_name: str,
    *,
    target_present_raw: int,
    max_res: int,
) -> dict[str, int]:
    # LeRobot's Feetech sign-magnitude encoder for Homing_Offset supports
    # -2047..2047. The STS-specific Torque_Enable=128 path is still preferred
    # for exact 2048 calibration at the rare raw-0/raw-4095 boundary.
    max_lerobot_homing_offset = int(max_res // 2)
    bus.write("Homing_Offset", joint_name, 0, normalize=False)
    bus.write("Min_Position_Limit", joint_name, 0, normalize=False)
    bus.write("Max_Position_Limit", joint_name, int(max_res), normalize=False)
    time.sleep(0.08)

    raw_with_zero_offset = int(bus.read("Present_Position", joint_name, normalize=False))
    homing_offset = int(raw_with_zero_offset) - int(target_present_raw)
    if abs(int(homing_offset)) > max_lerobot_homing_offset:
        raise ValueError(
            f"Homing_Offset {homing_offset} for {joint_name} exceeds the LeRobot encoder range "
            f"[-{max_lerobot_homing_offset}, {max_lerobot_homing_offset}]."
        )
    bus.write("Homing_Offset", joint_name, homing_offset, normalize=False)
    time.sleep(0.08)

    present_raw = int(bus.read("Present_Position", joint_name, normalize=False))
    return {
        "homing_offset": int(homing_offset),
        "present_raw": int(present_raw),
        "raw_with_zero_offset": int(raw_with_zero_offset),
    }


def _set_single_turn_current_pose_to_midpoint(bus, joints: list[str]) -> dict[str, dict[str, Any]]:
    """Use the STS current-position calibration command for 1/4.

    STS memory address 40 (`Torque_Enable`) accepts value 128, documented as
    correcting the arbitrary current position to raw 2048. Keeping this as the
    bounded-joint zero avoids placing runtime zero near the 0/4095 wrap seam.
    """

    results: dict[str, dict[str, Any]] = {}
    bus.disable_torque()
    for joint_name in joints:
        model = bus.motors[joint_name].model
        max_res = int(bus.model_resolution_table[model] - 1)
        target_zero_raw = _single_turn_zero_present_raw(max_res)

        bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)
        bus.write("Min_Position_Limit", joint_name, 0, normalize=False)
        bus.write("Max_Position_Limit", joint_name, int(max_res), normalize=False)
        bus.write("Torque_Enable", joint_name, 128, normalize=False)
        time.sleep(0.08)
        try:
            bus.write("Torque_Enable", joint_name, 0, normalize=False)
        except Exception:
            pass

        present_raw = int(bus.read("Present_Position", joint_name, normalize=False))
        homing_offset = _read_homing_offset(bus, joint_name)
        method = "torque_enable_128"
        raw_after_torque_enable_128 = int(present_raw)

        if int(present_raw) != int(target_zero_raw):
            try:
                exact_result = _set_homing_offset_for_current_pose(
                    bus,
                    joint_name,
                    target_present_raw=int(target_zero_raw),
                    max_res=int(max_res),
                )
                present_raw = int(exact_result["present_raw"])
                homing_offset = int(exact_result["homing_offset"])
                method = (
                    "torque_enable_128_then_exact_homing_offset"
                    if abs(raw_after_torque_enable_128 - int(target_zero_raw)) <= SINGLE_TURN_ZERO_TOLERANCE_RAW
                    else "exact_homing_offset_fallback"
                )
            except Exception:
                fallback_offsets = bus.set_half_turn_homings([joint_name])
                time.sleep(0.08)
                present_raw = int(bus.read("Present_Position", joint_name, normalize=False))
                homing_offset = int(fallback_offsets.get(joint_name, homing_offset))
                method = "set_half_turn_homings_last_resort"

        if abs(int(present_raw) - int(target_zero_raw)) > SINGLE_TURN_ZERO_TOLERANCE_RAW:
            raise RuntimeError(
                f"Failed to set {joint_name} current pose to midpoint raw {target_zero_raw}; "
                f"Present_Position is {present_raw}."
            )

        results[joint_name] = {
            "method": method,
            "homing_offset": int(homing_offset),
            "zero_present_raw": int(present_raw),
            "target_zero_present_raw": int(target_zero_raw),
            "raw_after_torque_enable_128": int(raw_after_torque_enable_128),
        }

    bus.disable_torque()
    return results


def _record_ranges_of_motion(
    bus,
    motor_names: list[str],
    *,
    display_values: bool,
    allow_static: set[str] | None = None,
) -> tuple[dict[str, int], dict[str, int], list[str]]:
    try:
        from lerobot.utils.utils import enter_pressed, move_cursor_up
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("Calibration range recording requires the optional 'lerobot' dependency.") from exc

    names = [str(name) for name in motor_names]
    static_ok = set(allow_static or set())
    start_positions = bus.sync_read("Present_Position", names, normalize=False)
    mins = {name: int(start_positions[name]) for name in names}
    maxes = dict(mins)

    user_pressed_enter = False
    while not user_pressed_enter:
        positions = {
            name: int(value)
            for name, value in bus.sync_read("Present_Position", names, normalize=False).items()
            if name in names
        }
        mins = {name: min(int(positions[name]), int(mins[name])) for name in names}
        maxes = {name: max(int(positions[name]), int(maxes[name])) for name in names}

        if display_values:
            print("\n-------------------------------------------")
            print(f"{'NAME':<15} | {'MIN':>6} | {'POS':>6} | {'MAX':>6}")
            for name in names:
                print(f"{name:<15} | {mins[name]:>6} | {positions[name]:>6} | {maxes[name]:>6}")

        user_pressed_enter = bool(enter_pressed())

        if display_values and not user_pressed_enter:
            move_cursor_up(len(names) + 3)

    same_min_max = [name for name in names if int(mins[name]) == int(maxes[name])]
    blocking_static = [name for name in same_min_max if name not in static_ok]
    if blocking_static:
        raise ValueError(
            "Some required motors have the same min and max values:\n"
            + "\n".join(f"- {name}" for name in blocking_static)
        )

    return mins, maxes, same_min_max


def _apply_absolute_raw_defaults(bus, current_hw_calib: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for joint_name in MULTI_TURN_JOINTS:
        _apply_multi_turn_fixed_config(
            bus,
            joint_name,
            {
                "phase": MULTI_TURN_PHASE_VALUE,
            },
        )
        snapshot = _read_joint_snapshot(bus, joint_name)
        entry, result_payload = _build_multi_turn_calibration_entry(
            current_cal=current_hw_calib[joint_name],
            home_present_raw=int(snapshot["position"]),
            home_present_wrapped_raw=int(snapshot["position_wrapped"]),
            min_present_raw=int(snapshot["position"]),
            max_present_raw=int(snapshot["position"]),
        )
        results[joint_name] = {
            "entry": entry,
            "result": result_payload,
        }
    return results


def _has_complete_single_turn_entries(payload: Mapping[str, Any]) -> bool:
    for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
        entry = payload.get(joint_name)
        if not isinstance(entry, Mapping):
            return False
        for field_name in ("id", "homing_offset", "range_min", "range_max", "zero_present_raw"):
            if field_name not in entry:
                return False
    return True


def _build_meta(*, generated_at: float, gripper_requested: bool, gripper_detected: bool) -> dict[str, Any]:
    return {
        "generated_at_unix_s": float(generated_at),
        "script": "soarmmoce_calibrate.py",
        "bounded_single_turn_joints": list(BOUNDED_SINGLE_TURN_JOINTS),
        "absolute_raw_joints": list(MULTI_TURN_JOINTS),
        "optional_gripper": {
            "requested": bool(gripper_requested),
            "detected": bool(gripper_detected),
            "joint": GRIPPER_JOINT_NAME if bool(gripper_detected) else None,
        },
        "notes": {
            "bounded_single_turn": "1/4 calibrate the selected zero pose to raw midpoint 2048, then record range limits around that midpoint.",
            "absolute_raw": "2/3/5 use mode 0 with min/max limit 0 and phase 28; startup pose is the runtime reference.",
            "optional_gripper": "If motor id 6 responds, gripper is calibrated like a bounded single-turn joint: current midpoint to raw 2048, then safe range capture.",
            "home": "home() returns 1/4 to calibrated midpoint zero and 2/3/5 to their startup-pose zero.",
        },
    }


def _calibrate(cfg: CalibrateConfig) -> Dict[str, Any]:
    robot_cfg = cfg.robot
    context = _resolve_paths(robot_cfg)
    target_source_path = context["target_source_path"]
    output_path = context["output_path"]
    target_robot_id = str(context["robot_id"])
    seed_payload = _read_json(target_source_path)
    if not isinstance(seed_payload, dict):
        seed_payload = {}
    gripper_requested = bool(context["gripper_available"])
    gripper_motor_id = _gripper_motor_id_from_payload(seed_payload)

    bus = _connect_calibration_bus(
        port=str(context["port"]),
        motor_model=str(robot_cfg.motor_model),
        timeout_s=float(robot_cfg.connect_timeout_s),
        include_gripper=gripper_requested,
        gripper_motor_id=gripper_motor_id,
    )
    try:
        current_hw_calib = bus.read_calibration()
        gripper_detected = _bus_has_gripper(bus)
        calibration_joint_names = list(JOINTS)
        if gripper_detected:
            calibration_joint_names.append(GRIPPER_JOINT_NAME)

        for joint_name in calibration_joint_names:
            bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)

        absolute_raw_results = _apply_absolute_raw_defaults(bus, current_hw_calib)
        gripper_entry: dict[str, Any] | None = None
        gripper_result: dict[str, Any] = {
            "requested": bool(gripper_requested),
            "detected": bool(gripper_detected),
            "motor_id": int(gripper_motor_id),
            "calibration_mode": "not_present",
        }
        if gripper_detected:
            gripper_entry, gripper_payload = _build_gripper_calibration_entry(
                current_hw_calib=current_hw_calib,
                seed_payload=seed_payload,
            )
            gripper_result.update(gripper_payload)

        bounded_entries: dict[str, dict[str, Any]] = {}
        bounded_results: dict[str, dict[str, Any]] = {}

        reuse_existing = False
        if bool(robot_cfg.prompt_existing) and _has_complete_single_turn_entries(seed_payload):
            user_input = input(
                f"Press ENTER to recalibrate robot id {target_robot_id} from the current startup pose, "
                "or type 'k' and press ENTER to keep the existing bounded single-turn calibration: "
            )
            reuse_existing = user_input.strip().lower() in {"k", "keep", "reuse"}

        if reuse_existing:
            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                entry = dict(seed_payload[joint_name])
                bounded_entries[joint_name] = entry
                bounded_results[joint_name] = {
                    "calibration_mode": "reuse_existing_single_turn",
                    "homing_offset": int(entry["homing_offset"]),
                    "range_min": int(entry["range_min"]),
                    "range_max": int(entry["range_max"]),
                    "zero_present_raw": int(entry.get("zero_present_raw", SINGLE_TURN_ZERO_RAW)),
                }
        else:
            range_joint_names = list(BOUNDED_SINGLE_TURN_JOINTS)
            if gripper_entry is not None:
                range_joint_names.append(GRIPPER_JOINT_NAME)

            print(
                "Torque is disabled. Move shoulder_pan / wrist_flex to the pose you want to be q=0"
                + (" and gripper to its midpoint" if gripper_entry is not None else "")
                + ".\n"
                "When you press ENTER, the script will correct that current pose to raw midpoint 2048. "
                "Do not try to calibrate shoulder_lift / elbow_flex / wrist_roll here; 2/3/5 will stay in fixed absolute-raw mode."
            )
            input()

            midpoint_joint_names = list(BOUNDED_SINGLE_TURN_JOINTS)
            if gripper_entry is not None:
                midpoint_joint_names.append(GRIPPER_JOINT_NAME)
            midpoint_homings = _set_single_turn_current_pose_to_midpoint(
                bus,
                midpoint_joint_names,
            )

            print(
                "Now move shoulder_pan / wrist_flex"
                + (" / gripper" if gripper_entry is not None else "")
                + " sequentially through their safe travel with torque disabled.\n"
                "Avoid crossing the 0/4095 seam; if the range crosses that seam, choose a different q=0 pose. "
                "Press ENTER again when the full range has been covered."
            )
            range_mins, range_maxes, static_motors = _record_ranges_of_motion(
                bus,
                range_joint_names,
                display_values=bool(robot_cfg.display_values),
                allow_static={GRIPPER_JOINT_NAME},
            )

            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                model = bus.motors[joint_name].model
                max_res = int(bus.model_resolution_table[model] - 1)
                entry, result_payload = _build_single_turn_calibration_entry(
                    current_cal=current_hw_calib[joint_name],
                    max_res=max_res,
                    homing_offset_raw=int(midpoint_homings[joint_name]["homing_offset"]),
                    zero_present_raw=int(midpoint_homings[joint_name]["zero_present_raw"]),
                    min_present_raw=int(range_mins[joint_name]),
                    max_present_raw=int(range_maxes[joint_name]),
                )
                result_payload["midpoint_method"] = str(midpoint_homings[joint_name]["method"])
                bounded_entries[joint_name] = entry
                bounded_results[joint_name] = result_payload

            if gripper_entry is not None:
                if GRIPPER_JOINT_NAME in static_motors:
                    gripper_result["range_capture"] = "not_moved_reused_existing"
                    gripper_result["midpoint_homing"] = midpoint_homings[GRIPPER_JOINT_NAME]
                    gripper_result["warning"] = (
                        "Gripper was not moved during range capture, so the previous gripper range was reused. "
                        "Move gripper through its full safe travel to replace any 0/4095 seam-crossing range."
                    )
                else:
                    model = bus.motors[GRIPPER_JOINT_NAME].model
                    max_res = int(bus.model_resolution_table[model] - 1)
                    gripper_entry, gripper_range_result = _build_single_turn_calibration_entry(
                        current_cal=current_hw_calib[GRIPPER_JOINT_NAME],
                        max_res=max_res,
                        homing_offset_raw=int(midpoint_homings[GRIPPER_JOINT_NAME]["homing_offset"]),
                        zero_present_raw=int(midpoint_homings[GRIPPER_JOINT_NAME]["zero_present_raw"]),
                        min_present_raw=int(range_mins[GRIPPER_JOINT_NAME]),
                        max_present_raw=int(range_maxes[GRIPPER_JOINT_NAME]),
                    )
                    gripper_range_result["midpoint_method"] = str(midpoint_homings[GRIPPER_JOINT_NAME]["method"])
                    gripper_result.update(gripper_range_result)

        written_json = dict(seed_payload)
        for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
            written_json[joint_name] = bounded_entries[joint_name]
        for joint_name in MULTI_TURN_JOINTS:
            written_json[joint_name] = absolute_raw_results[joint_name]["entry"]
        if gripper_entry is not None:
            written_json[GRIPPER_JOINT_NAME] = gripper_entry
        written_json["_meta"] = _build_meta(
            generated_at=time.time(),
            gripper_requested=gripper_requested,
            gripper_detected=gripper_detected,
        )

        if robot_cfg.apply_registers:
            bus.disable_torque()
            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                _apply_single_turn_calibration(bus, joint_name, bounded_entries[joint_name])
            for joint_name in MULTI_TURN_JOINTS:
                _apply_multi_turn_fixed_config(bus, joint_name, absolute_raw_results[joint_name]["entry"])
            if gripper_entry is not None:
                _apply_gripper_calibration(bus, gripper_entry)

        if robot_cfg.save_json:
            _write_json(output_path, written_json)

        urdf_limit_update: dict[str, Any] = {"updated": False, "written_limits": {}}
        if bool(robot_cfg.update_urdf_limits):
            urdf_limit_update = _write_bounded_single_turn_limits_to_urdf(
                urdf_path=Path(context["urdf_path"]),
                bounded_entries=bounded_entries,
                joint_scales=dict(context["joint_scales"]),
            )

        register_writes: dict[str, Any] = {
            **{
                joint_name: {
                    "operating_mode": POSITION_MODE_VALUE,
                    "homing_offset": int(bounded_entries[joint_name]["homing_offset"]),
                    "range_min": int(bounded_entries[joint_name]["range_min"]),
                    "range_max": int(bounded_entries[joint_name]["range_max"]),
                    "zero_present_raw": int(bounded_entries[joint_name].get("zero_present_raw", SINGLE_TURN_ZERO_RAW)),
                }
                for joint_name in BOUNDED_SINGLE_TURN_JOINTS
            },
            **{
                joint_name: {
                    "operating_mode": POSITION_MODE_VALUE,
                    "homing_offset": 0,
                    "phase": MULTI_TURN_PHASE_VALUE,
                    "range_min": MULTI_TURN_DISABLED_LIMIT_RAW,
                    "range_max": MULTI_TURN_DISABLED_LIMIT_RAW,
                }
                for joint_name in MULTI_TURN_JOINTS
            },
        }
        if gripper_entry is not None:
            register_writes[GRIPPER_JOINT_NAME] = {
                "operating_mode": POSITION_MODE_VALUE,
                "homing_offset": int(gripper_entry["homing_offset"]),
                "range_min": int(gripper_entry["range_min"]),
                "range_max": int(gripper_entry["range_max"]),
            }
            if "zero_present_raw" in gripper_entry:
                register_writes[GRIPPER_JOINT_NAME]["zero_present_raw"] = int(gripper_entry["zero_present_raw"])

        return {
            "action": "calibrate",
            "script": "soarmmoce_calibrate.py",
            "robot_id": target_robot_id,
            "port": str(context["port"]),
            "source_calibration_path": str(target_source_path),
            "output_path": str(output_path),
            "saved_json": bool(robot_cfg.save_json),
            "applied_registers": bool(robot_cfg.apply_registers),
            "urdf_limit_update": urdf_limit_update,
            "bounded_single_turn_joints": list(BOUNDED_SINGLE_TURN_JOINTS),
            "absolute_raw_joints": list(MULTI_TURN_JOINTS),
            "bounded_single_turn_result": bounded_results,
            "absolute_raw_result": {
                joint_name: absolute_raw_results[joint_name]["result"] for joint_name in MULTI_TURN_JOINTS
            },
            "gripper_result": gripper_result,
            "register_writes": register_writes,
        }
    finally:
        _disconnect_bus(bus)


def _run_main(cfg: CalibrateConfig) -> None:
    run_and_print(lambda: _calibrate(cfg))


if DRACCUS_AVAILABLE:

    @draccus.wrap()
    def main(cfg: CalibrateConfig) -> None:
        _run_main(cfg)

else:

    def main(cfg: CalibrateConfig | None = None) -> None:
        if cfg is None:
            cfg = CalibrateConfig()
        _run_main(cfg)


if __name__ == "__main__":
    main()
