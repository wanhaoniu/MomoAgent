#!/usr/bin/env python3
"""Interactive manual calibration for a standalone soarmMoce gripper servo."""

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import draccus
from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.utils.utils import enter_pressed, move_cursor_up

from soarmmoce_auto_calibrate import (
    DEFAULT_CONNECT_TIMEOUT_S,
    _build_single_turn_calibration_entry,
    _read_joint_snapshot,
    _read_json,
    _single_turn_zero_present_raw,
    _write_json,
)
from soarmmoce_cli_common import run_and_print
from soarmmoce_sdk import resolve_config


SUPPORTED_ROBOT_TYPES = {"soarmmoce"}
GRIPPER_JOINT_NAME = "gripper"


class _ManualConnectTimeout(RuntimeError):
    pass


@dataclass
class ManualGripperCalibrateRobotConfig:
    type: str = "soarmmoce"
    port: str = ""
    id: str = ""
    calib_dir: str = ""
    output: str = ""
    joint_name: str = GRIPPER_JOINT_NAME
    motor_id: int = 6
    motor_model: str = "sts3215"
    apply_registers: bool = True
    save_json: bool = True
    display_values: bool = True
    poll_interval_s: float = 0.05
    prompt_existing: bool = True

    def __post_init__(self) -> None:
        robot_type = str(self.type or "").strip().lower()
        if robot_type not in SUPPORTED_ROBOT_TYPES:
            supported = ", ".join(sorted(SUPPORTED_ROBOT_TYPES))
            raise ValueError(f"Unsupported robot.type={self.type!r}. Expected one of: {supported}")
        joint_name = str(self.joint_name or "").strip()
        if not joint_name:
            raise ValueError("robot.joint_name must not be empty")
        self.joint_name = joint_name
        if int(self.motor_id) <= 0:
            raise ValueError("robot.motor_id must be a positive integer")


@dataclass
class ManualGripperCalibrateConfig:
    robot: ManualGripperCalibrateRobotConfig = field(default_factory=ManualGripperCalibrateRobotConfig)


def _disconnect_bus(bus: FeetechMotorsBus | None) -> None:
    if bus is None:
        return
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def _connect_manual_calibration_bus(
    *,
    port: str,
    joint_name: str,
    motor_id: int,
    motor_model: str,
    timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> FeetechMotorsBus:
    bus = FeetechMotorsBus(
        port=port,
        motors={
            joint_name: Motor(int(motor_id), str(motor_model), MotorNormMode.DEGREES),
        },
    )

    timeout_s = float(timeout_s)
    previous_handler = None

    if timeout_s > 0.0:
        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _ManualConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to arm bus")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)

    try:
        bus.connect()
    except _ManualConnectTimeout as exc:
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

    try:
        bus.disable_torque()
    except Exception:
        _disconnect_bus(bus)
        raise

    return bus


def _resolve_paths(robot_cfg: ManualGripperCalibrateRobotConfig) -> Dict[str, Any]:
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


def _capture_home_pose(
    *,
    bus: FeetechMotorsBus,
    joint_name: str,
    poll_interval_s: float,
    display_values: bool,
) -> Dict[str, int]:
    captured = False
    current_values = {
        "position": 0,
        "position_wrapped": 0,
        "zero_ref": 0,
    }

    while not captured:
        snap = _read_joint_snapshot(bus, joint_name)
        model = bus.motors[joint_name].model
        max_res = int(bus.model_resolution_table[model] - 1)
        current_values = {
            "position": int(snap["position"]),
            "position_wrapped": int(snap["position_wrapped"]),
            "zero_ref": int(_single_turn_zero_present_raw(max_res)),
        }

        if display_values:
            print("\nPlace the gripper at the desired zero/reference pose, then press ENTER.")
            print(f"{'NAME':<15} | {'ACTUAL_RAW':>10} | {'WRAPPED':>8} | {'ZERO_REF':>10}")
            print(
                f"{joint_name:<15} | {current_values['position']:>10} | "
                f"{current_values['position_wrapped']:>8} | {current_values['zero_ref']:>10}"
            )

        if enter_pressed():
            captured = True
        else:
            time.sleep(max(0.01, float(poll_interval_s)))
            if display_values:
                move_cursor_up(3)

    return current_values


def _record_manual_range(
    *,
    bus: FeetechMotorsBus,
    joint_name: str,
    poll_interval_s: float,
    display_values: bool,
) -> Dict[str, int]:
    start_position = int(_read_joint_snapshot(bus, joint_name)["position"])
    range_min = start_position
    range_max = start_position
    finished = False

    while not finished:
        position = int(_read_joint_snapshot(bus, joint_name)["position"])
        range_min = min(range_min, position)
        range_max = max(range_max, position)

        if display_values:
            print("\nMove only the gripper through the full open/close range, then press ENTER.")
            print(f"{'NAME':<15} | {'MIN':>6} | {'POS':>6} | {'MAX':>6}")
            print(f"{joint_name:<15} | {range_min:>6} | {position:>6} | {range_max:>6}")

        if enter_pressed():
            finished = True
        else:
            time.sleep(max(0.01, float(poll_interval_s)))
            if display_values:
                move_cursor_up(3)

    if range_min == range_max:
        raise ValueError(f"{joint_name} did not move during manual range recording")

    return {
        "range_min": int(range_min),
        "range_max": int(range_max),
    }


def _apply_gripper_calibration(bus: FeetechMotorsBus, joint_name: str, entry: Dict[str, Any]) -> None:
    bus.write("Homing_Offset", joint_name, int(entry["homing_offset"]), normalize=False)
    bus.write("Min_Position_Limit", joint_name, int(entry["range_min"]), normalize=False)
    bus.write("Max_Position_Limit", joint_name, int(entry["range_max"]), normalize=False)


def _manual_calibrate(cfg: ManualGripperCalibrateConfig) -> Dict[str, Any]:
    robot_cfg = cfg.robot
    context = _resolve_paths(robot_cfg)
    joint_name = str(robot_cfg.joint_name)
    target_source_path = context["target_source_path"]
    output_path = context["output_path"]
    target_robot_id = str(context["robot_id"])

    target_calib_json = _read_json(target_source_path)
    seed_payload = dict(target_calib_json) if isinstance(target_calib_json, dict) else {}

    bus = _connect_manual_calibration_bus(
        port=str(context["port"]),
        joint_name=joint_name,
        motor_id=int(robot_cfg.motor_id),
        motor_model=str(robot_cfg.motor_model),
    )
    try:
        existing_entry = target_calib_json.get(joint_name) if isinstance(target_calib_json, dict) else None
        if isinstance(existing_entry, dict) and bool(robot_cfg.prompt_existing):
            user_input = input(
                f"Press ENTER to keep the existing {joint_name} calibration for robot id {target_robot_id}, "
                "or type 'c' and press ENTER to recalibrate: "
            )
            if user_input.strip().lower() != "c":
                if robot_cfg.apply_registers:
                    bus.disable_torque()
                    _apply_gripper_calibration(bus, joint_name, existing_entry)
                if robot_cfg.save_json and output_path != target_source_path:
                    _write_json(output_path, seed_payload)
                return {
                    "action": "manual_calibrate_gripper",
                    "mode": "use_existing_calibration",
                    "robot_id": target_robot_id,
                    "port": str(context["port"]),
                    "joint": joint_name,
                    "motor_id": int(robot_cfg.motor_id),
                    "source_calibration_path": str(target_source_path),
                    "output_path": str(output_path),
                    "saved_json": bool(robot_cfg.save_json and output_path != target_source_path),
                    "applied_registers": bool(robot_cfg.apply_registers),
                    "register_writes": {
                        "homing_offset": int(existing_entry["homing_offset"]),
                        "range_min": int(existing_entry["range_min"]),
                        "range_max": int(existing_entry["range_max"]),
                    },
                }

        current_hw_calib = bus.read_calibration()
        print(
            "Torque is disabled. Place the gripper at the desired reference pose for q=0, "
            "then press ENTER to capture it."
        )
        home_pose = _capture_home_pose(
            bus=bus,
            joint_name=joint_name,
            poll_interval_s=float(robot_cfg.poll_interval_s),
            display_values=bool(robot_cfg.display_values),
        )

        offsets = bus.set_half_turn_homings([joint_name])
        homing_offset_raw = int(offsets[joint_name])

        print(
            "Now move the gripper through its full close/open travel with torque disabled. "
            "Press ENTER again when the full range has been covered."
        )
        observed_range = _record_manual_range(
            bus=bus,
            joint_name=joint_name,
            poll_interval_s=float(robot_cfg.poll_interval_s),
            display_values=bool(robot_cfg.display_values),
        )

        model = bus.motors[joint_name].model
        max_res = int(bus.model_resolution_table[model] - 1)
        entry, result_payload = _build_single_turn_calibration_entry(
            current_cal=current_hw_calib[joint_name],
            max_res=max_res,
            homing_offset_raw=homing_offset_raw,
            min_present_raw=int(observed_range["range_min"]),
            max_present_raw=int(observed_range["range_max"]),
        )

        written_json = dict(seed_payload)
        written_json[joint_name] = entry

        if robot_cfg.apply_registers:
            bus.disable_torque()
            _apply_gripper_calibration(bus, joint_name, entry)

        if robot_cfg.save_json:
            _write_json(output_path, written_json)

        return {
            "action": "manual_calibrate_gripper",
            "mode": "interactive_manual",
            "robot_id": target_robot_id,
            "port": str(context["port"]),
            "joint": joint_name,
            "motor_id": int(robot_cfg.motor_id),
            "motor_model": str(robot_cfg.motor_model),
            "source_calibration_path": str(target_source_path),
            "output_path": str(output_path),
            "saved_json": bool(robot_cfg.save_json),
            "applied_registers": bool(robot_cfg.apply_registers),
            "result": {
                **result_payload,
                "captured_zero_raw_before_half_turn": int(home_pose["position"]),
                "captured_zero_wrapped_raw_before_half_turn": int(home_pose["position_wrapped"]),
            },
            "register_writes": {
                "homing_offset": int(entry["homing_offset"]),
                "range_min": int(entry["range_min"]),
                "range_max": int(entry["range_max"]),
            },
        }
    finally:
        _disconnect_bus(bus)


@draccus.wrap()
def main(cfg: ManualGripperCalibrateConfig) -> None:
    run_and_print(lambda: _manual_calibrate(cfg))


if __name__ == "__main__":
    main()
