#!/usr/bin/env python3
"""Calibrate bounded single-turn joints and normalize absolute-raw joint config."""

import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

try:
    import draccus
except ImportError:  # pragma: no cover - lightweight import fallback for tests
    class _DraccusCompat:
        @staticmethod
        def wrap():
            def _decorator(fn):
                return fn

            return _decorator

    draccus = _DraccusCompat()

from soarmmoce_cli_common import run_and_print
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

ARM_MOTOR_IDS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
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
    prompt_existing: bool = True
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
    return int(int(max_res) / 2)


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

    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(getattr(current_cal, "drive_mode", 0)),
        "homing_offset": int(homing_offset_raw),
        "range_min": int(range_min),
        "range_max": int(range_max),
        "operating_mode": POSITION_MODE_VALUE,
    }
    result_payload = {
        "calibration_mode": "single_turn_half_turn_homing",
        "homing_offset": int(homing_offset_raw),
        "range_min": int(range_min),
        "range_max": int(range_max),
        "zero_present_raw": int(_single_turn_zero_present_raw(max_res)),
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


def _connect_calibration_bus(*, port: str, motor_model: str, timeout_s: float):
    try:
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "Calibration requires the optional 'lerobot' dependency in the current Python environment."
        ) from exc

    bus = FeetechMotorsBus(
        port=port,
        motors={
            joint_name: Motor(ARM_MOTOR_IDS[joint_name], str(motor_model), MotorNormMode.DEGREES)
            for joint_name in JOINTS
        },
    )

    timeout_s = float(timeout_s)
    previous_handler = None

    if timeout_s > 0.0:
        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _CalibrationConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to arm bus")

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

    bus.disable_torque()
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
    }


def _apply_single_turn_calibration(bus, joint_name: str, entry: Mapping[str, Any]) -> None:
    bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)
    bus.write("Homing_Offset", joint_name, int(entry["homing_offset"]), normalize=False)
    bus.write("Min_Position_Limit", joint_name, int(entry["range_min"]), normalize=False)
    bus.write("Max_Position_Limit", joint_name, int(entry["range_max"]), normalize=False)


def _apply_multi_turn_fixed_config(bus, joint_name: str, entry: Mapping[str, Any]) -> None:
    bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)
    bus.write("Homing_Offset", joint_name, 0, normalize=False)
    bus.write("Phase", joint_name, int(entry["phase"]), normalize=False)
    bus.write("Min_Position_Limit", joint_name, MULTI_TURN_DISABLED_LIMIT_RAW, normalize=False)
    bus.write("Max_Position_Limit", joint_name, MULTI_TURN_DISABLED_LIMIT_RAW, normalize=False)


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
        for field_name in ("id", "homing_offset", "range_min", "range_max"):
            if field_name not in entry:
                return False
    return True


def _build_meta(*, generated_at: float) -> dict[str, Any]:
    return {
        "generated_at_unix_s": float(generated_at),
        "script": "soarmmoce_calibrate.py",
        "bounded_single_turn_joints": list(BOUNDED_SINGLE_TURN_JOINTS),
        "absolute_raw_joints": list(MULTI_TURN_JOINTS),
        "notes": {
            "bounded_single_turn": "1/4 use half-turn homing plus manually recorded range limits.",
            "absolute_raw": "2/3/5 use mode 0 with min/max limit 0 and phase 28; startup pose is the runtime reference.",
            "home": "home() returns every joint to zero relative angle, which is the startup pose reference.",
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

    bus = _connect_calibration_bus(
        port=str(context["port"]),
        motor_model=str(robot_cfg.motor_model),
        timeout_s=float(robot_cfg.connect_timeout_s),
    )
    try:
        current_hw_calib = bus.read_calibration()
        for joint_name in JOINTS:
            bus.write("Operating_Mode", joint_name, POSITION_MODE_VALUE, normalize=False)

        absolute_raw_results = _apply_absolute_raw_defaults(bus, current_hw_calib)

        bounded_entries: dict[str, dict[str, Any]] = {}
        bounded_results: dict[str, dict[str, Any]] = {}

        reuse_existing = False
        if bool(robot_cfg.prompt_existing) and _has_complete_single_turn_entries(seed_payload):
            user_input = input(
                f"Press ENTER to keep the existing bounded single-turn calibration for robot id {target_robot_id}, "
                "or type 'c' and press ENTER to recalibrate 1/4: "
            )
            reuse_existing = user_input.strip().lower() != "c"

        if reuse_existing:
            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                entry = dict(seed_payload[joint_name])
                bounded_entries[joint_name] = entry
                bounded_results[joint_name] = {
                    "calibration_mode": "reuse_existing_single_turn",
                    "homing_offset": int(entry["homing_offset"]),
                    "range_min": int(entry["range_min"]),
                    "range_max": int(entry["range_max"]),
                }
        else:
            print(
                "Torque is disabled. Move only shoulder_pan / wrist_flex to the middle of their safe range,\n"
                "then press ENTER. Do not try to calibrate shoulder_lift / elbow_flex / wrist_roll here; 2/3/5 will stay in fixed absolute-raw mode."
            )
            input()

            homing_offsets = bus.set_half_turn_homings(list(BOUNDED_SINGLE_TURN_JOINTS))

            print(
                "Now move shoulder_pan / wrist_flex sequentially through their safe travel with torque disabled.\n"
                "Press ENTER again when the full range has been covered."
            )
            range_mins, range_maxes = bus.record_ranges_of_motion(
                list(BOUNDED_SINGLE_TURN_JOINTS),
                display_values=bool(robot_cfg.display_values),
            )

            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                model = bus.motors[joint_name].model
                max_res = int(bus.model_resolution_table[model] - 1)
                entry, result_payload = _build_single_turn_calibration_entry(
                    current_cal=current_hw_calib[joint_name],
                    max_res=max_res,
                    homing_offset_raw=int(homing_offsets[joint_name]),
                    min_present_raw=int(range_mins[joint_name]),
                    max_present_raw=int(range_maxes[joint_name]),
                )
                bounded_entries[joint_name] = entry
                bounded_results[joint_name] = result_payload

        written_json = dict(seed_payload)
        for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
            written_json[joint_name] = bounded_entries[joint_name]
        for joint_name in MULTI_TURN_JOINTS:
            written_json[joint_name] = absolute_raw_results[joint_name]["entry"]
        written_json["_meta"] = _build_meta(
            generated_at=time.time(),
        )

        if robot_cfg.apply_registers:
            bus.disable_torque()
            for joint_name in BOUNDED_SINGLE_TURN_JOINTS:
                _apply_single_turn_calibration(bus, joint_name, bounded_entries[joint_name])
            for joint_name in MULTI_TURN_JOINTS:
                _apply_multi_turn_fixed_config(bus, joint_name, absolute_raw_results[joint_name]["entry"])

        if robot_cfg.save_json:
            _write_json(output_path, written_json)

        return {
            "action": "calibrate",
            "script": "soarmmoce_calibrate.py",
            "robot_id": target_robot_id,
            "port": str(context["port"]),
            "source_calibration_path": str(target_source_path),
            "output_path": str(output_path),
            "saved_json": bool(robot_cfg.save_json),
            "applied_registers": bool(robot_cfg.apply_registers),
            "bounded_single_turn_joints": list(BOUNDED_SINGLE_TURN_JOINTS),
            "absolute_raw_joints": list(MULTI_TURN_JOINTS),
            "bounded_single_turn_result": bounded_results,
            "absolute_raw_result": {
                joint_name: absolute_raw_results[joint_name]["result"] for joint_name in MULTI_TURN_JOINTS
            },
            "register_writes": {
                **{
                    joint_name: {
                        "operating_mode": POSITION_MODE_VALUE,
                        "homing_offset": int(bounded_entries[joint_name]["homing_offset"]),
                        "range_min": int(bounded_entries[joint_name]["range_min"]),
                        "range_max": int(bounded_entries[joint_name]["range_max"]),
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
            },
        }
    finally:
        _disconnect_bus(bus)


@draccus.wrap()
def main(cfg: CalibrateConfig) -> None:
    run_and_print(lambda: _calibrate(cfg))


if __name__ == "__main__":
    main()
