#!/usr/bin/env python3
"""Standalone CLI for controlling the soarmMoce gripper servo on motor id 6."""

import argparse
import json
import signal
import time
from pathlib import Path
from typing import Any, Dict

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode

from soarmmoce_cli_common import cli_bool, print_error, print_success
from soarmmoce_sdk import resolve_config


GRIPPER_JOINT_NAME = "gripper"
DEFAULT_MOTOR_ID = 6
DEFAULT_MOTOR_MODEL = "sts3215"
RAW_COUNTS_PER_REV = 4096
DEFAULT_CONNECT_TIMEOUT_S = 8.0
DEFAULT_SETTLE_TOLERANCE_RAW = 12
DEFAULT_POLL_INTERVAL_S = 0.02


class _ConnectTimeout(RuntimeError):
    pass


def _read_calibration_entry(*, robot_id: str, calib_dir: Path, joint_name: str) -> Dict[str, Any]:
    path = calib_dir / f"{robot_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Calibration file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Calibration file must contain a JSON object: {path}")
    entry = payload.get(joint_name)
    if not isinstance(entry, dict):
        raise KeyError(f"Calibration entry '{joint_name}' is missing from {path}")
    for field in ("id", "homing_offset", "range_min", "range_max"):
        if field not in entry:
            raise KeyError(f"Calibration entry '{joint_name}' missing required field: {field}")
    return {
        "path": str(path),
        "joint_name": joint_name,
        "id": int(entry["id"]),
        "drive_mode": int(entry.get("drive_mode", 0)),
        "homing_offset": int(entry["homing_offset"]),
        "range_min": int(entry["range_min"]),
        "range_max": int(entry["range_max"]),
    }


def _disconnect_bus(bus: FeetechMotorsBus | None) -> None:
    if bus is None:
        return
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def _connect_bus(*, port: str, motor_id: int, motor_model: str, timeout_s: float) -> FeetechMotorsBus:
    bus = FeetechMotorsBus(
        port=port,
        motors={
            GRIPPER_JOINT_NAME: Motor(int(motor_id), str(motor_model), MotorNormMode.DEGREES),
        },
    )

    timeout_s = float(timeout_s)
    previous_handler = None
    if timeout_s > 0.0:
        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _ConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to gripper bus")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)

    try:
        bus.connect()
    except _ConnectTimeout as exc:
        _disconnect_bus(bus)
        raise RuntimeError(
            f"{exc}. Check SOARMMOCE_PORT, power, and whether another process is using the serial port."
        ) from exc
    except Exception:
        _disconnect_bus(bus)
        raise
    finally:
        if timeout_s > 0.0:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)

    return bus


def _apply_gripper_registers(bus: FeetechMotorsBus, calibration: Dict[str, Any]) -> None:
    joint = calibration["joint_name"]
    bus.disable_torque()
    bus.write("Lock", joint, 0, normalize=False)
    bus.write("Operating_Mode", joint, OperatingMode.POSITION.value, normalize=False)
    bus.write("Homing_Offset", joint, int(calibration["homing_offset"]), normalize=False)
    bus.write("Min_Position_Limit", joint, int(calibration["range_min"]), normalize=False)
    bus.write("Max_Position_Limit", joint, int(calibration["range_max"]), normalize=False)
    bus.write("Lock", joint, 1, normalize=False)
    bus.enable_torque()


def _wrap_position_raw(raw_value: int | float) -> int:
    return int(raw_value) % RAW_COUNTS_PER_REV


def _read_present_raw(bus: FeetechMotorsBus, *, joint_name: str, homing_offset: int) -> Dict[str, Any]:
    register_raw = int(bus.read("Present_Position", joint_name, normalize=False))
    adjusted_raw = _wrap_position_raw(register_raw + int(homing_offset))
    return {
        "register_raw": int(register_raw),
        "adjusted_raw": int(adjusted_raw),
        "moving": int(bus.read("Moving", joint_name, normalize=False)),
        "velocity": float(bus.read("Present_Velocity", joint_name, normalize=False)),
        "current": float(bus.read("Present_Current", joint_name, normalize=False)),
    }


def _ratio_to_goal_raw(open_ratio: float, calibration: Dict[str, Any]) -> int:
    ratio = float(open_ratio)
    if ratio < 0.0 or ratio > 1.0:
        raise ValueError(f"open_ratio must be within [0.0, 1.0], got {open_ratio!r}")
    range_min = int(calibration["range_min"])
    range_max = int(calibration["range_max"])
    return int(round(range_min + ratio * (range_max - range_min)))


def _goal_to_ratio(goal_raw: int, calibration: Dict[str, Any]) -> float:
    range_min = float(calibration["range_min"])
    range_max = float(calibration["range_max"])
    if range_max <= range_min:
        return 0.0
    return float((float(goal_raw) - range_min) / (range_max - range_min))


def _wait_until_settled(
    *,
    bus: FeetechMotorsBus,
    calibration: Dict[str, Any],
    goal_raw: int,
    timeout: float | None,
    settle_tolerance_raw: int,
    poll_interval_s: float,
) -> Dict[str, Any]:
    joint = calibration["joint_name"]
    deadline = None if timeout is None else time.time() + max(0.0, float(timeout))
    while True:
        present = _read_present_raw(
            bus,
            joint_name=joint,
            homing_offset=int(calibration["homing_offset"]),
        )
        error_raw = int(goal_raw) - int(present["adjusted_raw"])
        if abs(error_raw) <= int(settle_tolerance_raw):
            return {
                "settled": True,
                "present_raw": int(present["adjusted_raw"]),
                "present_register_raw": int(present["register_raw"]),
                "error_raw": int(error_raw),
                "moving": int(present["moving"]),
                "velocity": float(present["velocity"]),
                "current": float(present["current"]),
            }
        if deadline is not None and time.time() > deadline:
            return {
                "settled": False,
                "present_raw": int(present["adjusted_raw"]),
                "present_register_raw": int(present["register_raw"]),
                "error_raw": int(error_raw),
                "moving": int(present["moving"]),
                "velocity": float(present["velocity"]),
                "current": float(present["current"]),
            }
        time.sleep(max(0.005, float(poll_interval_s)))


def _command_gripper(
    *,
    open_ratio: float,
    wait: bool,
    timeout: float | None,
    port: str | None,
    motor_id: int,
    motor_model: str,
    settle_tolerance_raw: int,
    poll_interval_s: float,
) -> Dict[str, Any]:
    config = resolve_config()
    target_port = str(port or config.port).strip()
    if not target_port:
        raise ValueError("Target port must not be empty")

    calibration = _read_calibration_entry(
        robot_id=str(config.robot_id),
        calib_dir=Path(config.calib_dir),
        joint_name=GRIPPER_JOINT_NAME,
    )
    if int(calibration["id"]) != int(motor_id):
        raise ValueError(
            f"Calibration expects motor id {calibration['id']} for '{GRIPPER_JOINT_NAME}', got {motor_id}"
        )

    goal_raw = _ratio_to_goal_raw(open_ratio, calibration)
    bus = _connect_bus(
        port=target_port,
        motor_id=int(motor_id),
        motor_model=str(motor_model),
        timeout_s=DEFAULT_CONNECT_TIMEOUT_S,
    )
    try:
        _apply_gripper_registers(bus, calibration)
        bus.write("Goal_Position", GRIPPER_JOINT_NAME, int(goal_raw), normalize=False)

        settled_result = None
        if wait:
            settled_result = _wait_until_settled(
                bus=bus,
                calibration=calibration,
                goal_raw=int(goal_raw),
                timeout=timeout,
                settle_tolerance_raw=int(settle_tolerance_raw),
                poll_interval_s=float(poll_interval_s),
            )
        else:
            present = _read_present_raw(
                bus,
                joint_name=GRIPPER_JOINT_NAME,
                homing_offset=int(calibration["homing_offset"]),
            )
            settled_result = {
                "settled": None,
                "present_raw": int(present["adjusted_raw"]),
                "present_register_raw": int(present["register_raw"]),
                "error_raw": int(goal_raw) - int(present["adjusted_raw"]),
                "moving": int(present["moving"]),
                "velocity": float(present["velocity"]),
                "current": float(present["current"]),
            }

        return {
            "action": "set_gripper",
            "port": target_port,
            "motor_id": int(motor_id),
            "motor_model": str(motor_model),
            "calibration_path": str(calibration["path"]),
            "goal_open_ratio": float(open_ratio),
            "goal_raw": int(goal_raw),
            "range_min": int(calibration["range_min"]),
            "range_max": int(calibration["range_max"]),
            "homing_offset": int(calibration["homing_offset"]),
            "wait": bool(wait),
            "timeout": None if timeout is None else float(timeout),
            "settle_tolerance_raw": int(settle_tolerance_raw),
            "state": {
                **settled_result,
                "present_open_ratio": _goal_to_ratio(int(settled_result["present_raw"]), calibration),
            },
        }
    finally:
        _disconnect_bus(bus)


def _read_gripper_state(
    *,
    port: str | None,
    motor_id: int,
    motor_model: str,
) -> Dict[str, Any]:
    config = resolve_config()
    target_port = str(port or config.port).strip()
    if not target_port:
        raise ValueError("Target port must not be empty")

    calibration = _read_calibration_entry(
        robot_id=str(config.robot_id),
        calib_dir=Path(config.calib_dir),
        joint_name=GRIPPER_JOINT_NAME,
    )
    if int(calibration["id"]) != int(motor_id):
        raise ValueError(
            f"Calibration expects motor id {calibration['id']} for '{GRIPPER_JOINT_NAME}', got {motor_id}"
        )

    bus = _connect_bus(
        port=target_port,
        motor_id=int(motor_id),
        motor_model=str(motor_model),
        timeout_s=DEFAULT_CONNECT_TIMEOUT_S,
    )
    try:
        _apply_gripper_registers(bus, calibration)
        present = _read_present_raw(
            bus,
            joint_name=GRIPPER_JOINT_NAME,
            homing_offset=int(calibration["homing_offset"]),
        )
        return {
            "action": "read_gripper",
            "port": target_port,
            "motor_id": int(motor_id),
            "motor_model": str(motor_model),
            "calibration_path": str(calibration["path"]),
            "range_min": int(calibration["range_min"]),
            "range_max": int(calibration["range_max"]),
            "homing_offset": int(calibration["homing_offset"]),
            "state": {
                "present_raw": int(present["adjusted_raw"]),
                "present_register_raw": int(present["register_raw"]),
                "present_open_ratio": _goal_to_ratio(int(present["adjusted_raw"]), calibration),
                "moving": int(present["moving"]),
                "velocity": float(present["velocity"]),
                "current": float(present["current"]),
            },
        }
    finally:
        _disconnect_bus(bus)


def main() -> None:
    parser = argparse.ArgumentParser(description="soarmMoce gripper CLI for standalone motor id 6")
    parser.add_argument("--port", default="", help="Serial port; defaults to SOARMMOCE_PORT / resolved config")
    parser.add_argument("--motor-id", type=int, default=DEFAULT_MOTOR_ID)
    parser.add_argument("--motor-model", default=DEFAULT_MOTOR_MODEL)

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Set gripper open ratio")
    p_set.add_argument("--open-ratio", type=float, required=True)
    p_set.add_argument("--wait", type=cli_bool, default=True)
    p_set.add_argument("--timeout", type=float, default=2.0)
    p_set.add_argument("--settle-tolerance-raw", type=int, default=DEFAULT_SETTLE_TOLERANCE_RAW)
    p_set.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)

    p_open = sub.add_parser("open", help="Open gripper fully")
    p_open.add_argument("--wait", type=cli_bool, default=True)
    p_open.add_argument("--timeout", type=float, default=2.0)
    p_open.add_argument("--settle-tolerance-raw", type=int, default=DEFAULT_SETTLE_TOLERANCE_RAW)
    p_open.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)

    p_close = sub.add_parser("close", help="Close gripper fully")
    p_close.add_argument("--wait", type=cli_bool, default=True)
    p_close.add_argument("--timeout", type=float, default=2.0)
    p_close.add_argument("--settle-tolerance-raw", type=int, default=DEFAULT_SETTLE_TOLERANCE_RAW)
    p_close.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)

    sub.add_parser("read", help="Read current gripper state")

    args = parser.parse_args()

    try:
        common = {
            "port": args.port or None,
            "motor_id": int(args.motor_id),
            "motor_model": str(args.motor_model),
        }
        if args.cmd == "set":
            result = _command_gripper(
                open_ratio=float(args.open_ratio),
                wait=bool(args.wait),
                timeout=args.timeout,
                settle_tolerance_raw=int(args.settle_tolerance_raw),
                poll_interval_s=float(args.poll_interval),
                **common,
            )
        elif args.cmd == "open":
            result = _command_gripper(
                open_ratio=1.0,
                wait=bool(args.wait),
                timeout=args.timeout,
                settle_tolerance_raw=int(args.settle_tolerance_raw),
                poll_interval_s=float(args.poll_interval),
                **common,
            )
        elif args.cmd == "close":
            result = _command_gripper(
                open_ratio=0.0,
                wait=bool(args.wait),
                timeout=args.timeout,
                settle_tolerance_raw=int(args.settle_tolerance_raw),
                poll_interval_s=float(args.poll_interval),
                **common,
            )
        else:
            result = _read_gripper_state(**common)
        print_success(result)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
