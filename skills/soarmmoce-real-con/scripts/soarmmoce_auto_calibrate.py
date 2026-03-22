#!/usr/bin/env python3
"""Automatic calibration for the 5-servo soarmMoce real arm."""

from __future__ import annotations

import argparse
import json
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from lerobot.motors.feetech import OperatingMode

from soarmmoce_cli_common import cli_bool, run_and_print
from soarmmoce_sdk import (
    JOINTS,
    MULTI_TURN_JOINTS,
    SKILL_ROOT,
    SoArmMoceController,
    resolve_config,
)


RAW_COUNTS_PER_REV = 4096
DEFAULT_MULTI_TURN_HOME_TOLERANCE_RAW = 96
HALF_RAW_COUNTS_PER_REV = RAW_COUNTS_PER_REV / 2.0
DEFAULT_CONNECT_TIMEOUT_S = 8.0
CALIBRATION_META_KEY = "_meta"


@dataclass(slots=True)
class MultiTurnTrackerState:
    last_wrapped_raw: int
    continuous_raw: int


class _ConnectTimeout(RuntimeError):
    pass


def _parse_joints(raw: str) -> list[str]:
    joints = []
    for item in str(raw or "").split(","):
        name = item.strip()
        if not name:
            continue
        if name not in JOINTS:
            raise argparse.ArgumentTypeError(f"unknown joint: {name}")
        joints.append(name)
    if not joints:
        raise argparse.ArgumentTypeError("at least one joint is required")
    return joints


def _ensure_bus_with_timeout(arm: SoArmMoceController, timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S):
    timeout_s = float(timeout_s)
    if timeout_s <= 0.0:
        return arm._ensure_bus()

    def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
        raise _ConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to arm bus")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return arm._ensure_bus()
    except _ConnectTimeout as exc:
        arm.close()
        raise RuntimeError(
            f"{exc}. The script is hanging during serial handshake; check SOARMMOCE_PORT, power, and port occupancy."
        ) from exc
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _wrap_position_raw(raw_value: int | float) -> int:
    return int(raw_value) % RAW_COUNTS_PER_REV


def _unwrap_position_raw(joint: str, wrapped_raw: int, tracker: Dict[str, MultiTurnTrackerState] | None) -> int:
    wrapped = _wrap_position_raw(wrapped_raw)
    if tracker is None or joint not in MULTI_TURN_JOINTS:
        return int(wrapped_raw)
    state = tracker.get(joint)
    if state is None:
        state = MultiTurnTrackerState(last_wrapped_raw=wrapped, continuous_raw=wrapped)
        tracker[joint] = state
        return int(state.continuous_raw)
    else:
        delta = int(wrapped) - int(state.last_wrapped_raw)
        if delta > HALF_RAW_COUNTS_PER_REV:
            delta -= RAW_COUNTS_PER_REV
        elif delta < -HALF_RAW_COUNTS_PER_REV:
            delta += RAW_COUNTS_PER_REV
        state.last_wrapped_raw = int(wrapped)
        state.continuous_raw = int(round(int(state.continuous_raw) + delta))
    return int(state.continuous_raw)


def _read_joint_snapshot(
    bus,
    joint: str,
    tracker: Dict[str, MultiTurnTrackerState] | None = None,
    homing_offset_raw: int = 0,
) -> Dict[str, Any]:
    register_raw = int(bus.read("Present_Position", joint, normalize=False))
    wrapped_raw = _wrap_position_raw(register_raw + int(homing_offset_raw))
    position_key = _unwrap_position_raw(joint, wrapped_raw, tracker)
    return {
        "position": int(position_key),
        "position_wrapped": int(wrapped_raw),
        "position_register_raw": int(register_raw),
        "velocity": float(bus.read("Present_Velocity", joint, normalize=False)),
        "moving": int(bus.read("Moving", joint, normalize=False)),
        "current": float(bus.read("Present_Current", joint, normalize=False)),
    }


def _configure_joint_for_calibration(bus, joint: str) -> Dict[str, int]:
    model = bus.motors[joint].model
    max_res = int(bus.model_resolution_table[model] - 1)
    if joint in MULTI_TURN_JOINTS:
        bus.write("Homing_Offset", joint, 0, normalize=False)
    bus.write("Min_Position_Limit", joint, 0, normalize=False)
    bus.write("Max_Position_Limit", joint, max_res, normalize=False)
    bus.write("Operating_Mode", joint, OperatingMode.POSITION.value, normalize=False)
    time.sleep(0.02)
    current_raw = _wrap_position_raw(bus.read("Present_Position", joint, normalize=False))
    bus.write("Goal_Position", joint, current_raw, normalize=False)
    return {"max_res": int(max_res), "current_wrapped_raw": int(current_raw)}


def _command_goal_from_reference(bus, joint: str, direction: int, step_raw: int, reference_position_raw: int) -> Dict[str, Any]:
    direction = 1 if direction >= 0 else -1
    step_raw = max(1, int(step_raw))
    if joint in MULTI_TURN_JOINTS:
        goal_value = _wrap_position_raw(int(reference_position_raw) + direction * step_raw)
    else:
        goal_value = int(min(4095, max(0, int(reference_position_raw) + direction * step_raw)))
    bus.write("Goal_Position", joint, goal_value, normalize=False)
    return {
        "kind": "wrapped_absolute_goal" if joint in MULTI_TURN_JOINTS else "absolute_goal",
        "goal_value": int(goal_value),
        "from_position": int(reference_position_raw),
    }


def _hold_joint(bus, joint: str, reference_position_raw: int | None = None) -> None:
    if reference_position_raw is None:
        reference_position_raw = int(bus.read("Present_Position", joint, normalize=False))
    hold_value = _wrap_position_raw(reference_position_raw) if joint in MULTI_TURN_JOINTS else int(reference_position_raw)
    bus.write("Goal_Position", joint, int(hold_value), normalize=False)


def _is_limit_fault(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    return any(token in message for token in ("overele", "over current", "overcurrent", "overload", "protect"))


def _backoff_from_limit(
    *,
    bus,
    joint: str,
    approach_direction: int,
    retreat_step_raw: int,
    poll_interval_s: float,
    attempts: int,
    reference_position_raw: int,
    tracker: Dict[str, MultiTurnTrackerState] | None,
) -> Dict[str, Any]:
    last_exc: Exception | None = None
    last_reference = int(reference_position_raw)
    for _ in range(max(1, int(attempts))):
        _command_goal_from_reference(
            bus,
            joint,
            -int(approach_direction),
            max(1, int(retreat_step_raw)),
            last_reference,
        )
        time.sleep(max(0.02, float(poll_interval_s)))
        try:
            snap = _read_joint_snapshot(bus, joint, tracker=tracker)
            _hold_joint(bus, joint, reference_position_raw=int(snap["position_wrapped"]))
            return snap
        except Exception as exc:  # pragma: no cover - hardware path
            last_exc = exc
            if joint not in MULTI_TURN_JOINTS:
                last_reference = int(
                    min(4095, max(0, last_reference - int(approach_direction) * max(1, int(retreat_step_raw))))
                )
    if last_exc is not None:
        raise RuntimeError(f"Failed to back off {joint} after limit fault: {last_exc}") from last_exc
    raise RuntimeError(f"Failed to back off {joint} after limit fault")


def _seek_limit(
    *,
    bus,
    joint: str,
    direction: int,
    step_raw: int,
    poll_interval_s: float,
    velocity_abs_threshold: float,
    movement_abs_threshold: int,
    settle_samples: int,
    stall_current_abs_threshold: float,
    timeout_s: float,
    tracker: Dict[str, MultiTurnTrackerState] | None,
) -> Dict[str, Any]:
    start_ts = time.time()
    pos_hist: deque[int] = deque(maxlen=max(2, int(settle_samples)))
    samples: list[Dict[str, Any]] = []
    max_abs_current = 0.0
    direction_name = "positive" if direction >= 0 else "negative"
    release_step_raw = max(int(step_raw), int(step_raw) * 2)
    last_snap = _read_joint_snapshot(bus, joint, tracker=tracker)
    pos_hist.append(int(last_snap["position"]))

    while True:
        cmd_info = _command_goal_from_reference(
            bus,
            joint,
            direction,
            step_raw,
            int(last_snap["position_wrapped"]),
        )
        time.sleep(max(0.01, float(poll_interval_s)))
        try:
            snap = _read_joint_snapshot(bus, joint, tracker=tracker)
        except Exception as exc:
            if not _is_limit_fault(exc):
                raise
            recovered = _backoff_from_limit(
                bus=bus,
                joint=joint,
                approach_direction=direction,
                retreat_step_raw=release_step_raw,
                poll_interval_s=poll_interval_s,
                attempts=max(2, int(settle_samples)),
                reference_position_raw=int(last_snap["position_wrapped"]),
                tracker=tracker,
            )
            return {
                "joint": joint,
                "direction": direction_name,
                "reason": "status_fault_fallback",
                "fault": str(exc),
                "limit_present_raw": int(last_snap["position"]),
                "limit_present_wrapped_raw": int(last_snap["position_wrapped"]),
                "release_present_raw": int(recovered["position"]),
                "release_present_wrapped_raw": int(recovered["position_wrapped"]),
                "max_abs_current": float(max_abs_current),
                "samples": samples,
            }
        pos_hist.append(int(snap["position"]))
        max_abs_current = max(max_abs_current, abs(float(snap["current"])))

        pos_span = 0 if len(pos_hist) < 2 else max(pos_hist) - min(pos_hist)
        low_velocity = abs(float(snap["velocity"])) <= float(velocity_abs_threshold)
        not_moving = int(snap["moving"]) == 0
        barely_moving = pos_span <= int(movement_abs_threshold)
        stall_current = (
            float(stall_current_abs_threshold) > 0.0
            and abs(float(snap["current"])) >= float(stall_current_abs_threshold)
        )
        multi_turn_stall = joint in MULTI_TURN_JOINTS and low_velocity and stall_current
        timeout_limit_like = low_velocity and (
            stall_current
            or (
                joint in MULTI_TURN_JOINTS
                and float(stall_current_abs_threshold) > 0.0
                and max_abs_current >= float(stall_current_abs_threshold) * 0.85
            )
        )

        sample = {
            "t": float(time.time() - start_ts),
            "position": int(snap["position"]),
            "velocity": float(snap["velocity"]),
            "moving": int(snap["moving"]),
            "current": float(snap["current"]),
            "pos_span": int(pos_span),
            "goal_kind": cmd_info["kind"],
            "goal_value": int(cmd_info["goal_value"]),
        }
        if len(samples) < 48:
            samples.append(sample)

        if len(pos_hist) >= max(2, int(settle_samples)) and low_velocity and not_moving and barely_moving:
            _hold_joint(bus, joint, reference_position_raw=int(snap["position_wrapped"]))
            recovered = _backoff_from_limit(
                bus=bus,
                joint=joint,
                approach_direction=direction,
                retreat_step_raw=release_step_raw,
                poll_interval_s=poll_interval_s,
                attempts=1,
                reference_position_raw=int(snap["position_wrapped"]),
                tracker=tracker,
            )
            return {
                "joint": joint,
                "direction": direction_name,
                "reason": "velocity_and_moving_register",
                "limit_present_raw": int(snap["position"]),
                "limit_present_wrapped_raw": int(snap["position_wrapped"]),
                "release_present_raw": int(recovered["position"]),
                "release_present_wrapped_raw": int(recovered["position_wrapped"]),
                "max_abs_current": float(max_abs_current),
                "samples": samples,
            }

        if len(pos_hist) >= max(2, int(settle_samples)) and (
            (stall_current and barely_moving) or multi_turn_stall
        ):
            _hold_joint(bus, joint, reference_position_raw=int(snap["position_wrapped"]))
            recovered = _backoff_from_limit(
                bus=bus,
                joint=joint,
                approach_direction=direction,
                retreat_step_raw=release_step_raw,
                poll_interval_s=poll_interval_s,
                attempts=max(2, int(settle_samples)),
                reference_position_raw=int(snap["position_wrapped"]),
                tracker=tracker,
            )
            return {
                "joint": joint,
                "direction": direction_name,
                "reason": "stall_current_fallback",
                "limit_present_raw": int(snap["position"]),
                "limit_present_wrapped_raw": int(snap["position_wrapped"]),
                "release_present_raw": int(recovered["position"]),
                "release_present_wrapped_raw": int(recovered["position_wrapped"]),
                "max_abs_current": float(max_abs_current),
                "samples": samples,
            }

        if time.time() - start_ts > float(timeout_s):
            if timeout_limit_like:
                _hold_joint(bus, joint, reference_position_raw=int(snap["position_wrapped"]))
                recovered = _backoff_from_limit(
                    bus=bus,
                    joint=joint,
                    approach_direction=direction,
                    retreat_step_raw=release_step_raw,
                    poll_interval_s=poll_interval_s,
                    attempts=max(2, int(settle_samples)),
                    reference_position_raw=int(snap["position_wrapped"]),
                    tracker=tracker,
                )
                return {
                    "joint": joint,
                    "direction": direction_name,
                    "reason": "timeout_limit_fallback",
                    "limit_present_raw": int(snap["position"]),
                    "limit_present_wrapped_raw": int(snap["position_wrapped"]),
                    "release_present_raw": int(recovered["position"]),
                    "release_present_wrapped_raw": int(recovered["position_wrapped"]),
                    "max_abs_current": float(max_abs_current),
                    "samples": samples,
                }
            _hold_joint(bus, joint, reference_position_raw=int(snap["position_wrapped"]))
            raise TimeoutError(
                f"Timed out while seeking {direction_name} limit for {joint}. "
                f"Last snapshot: pos={snap['position']} vel={snap['velocity']} moving={snap['moving']} current={snap['current']}"
            )
        last_snap = snap


def _move_joint_back_to_target(
    *,
    bus,
    joint: str,
    target_present_raw: int,
    step_raw: int,
    poll_interval_s: float,
    timeout_s: float,
    position_tolerance_raw: int = 6,
    tracker: Dict[str, MultiTurnTrackerState] | None = None,
) -> Dict[str, Any]:
    start_ts = time.time()
    last_snap = _read_joint_snapshot(bus, joint, tracker=tracker)
    while True:
        error = int(target_present_raw) - int(last_snap["position"])
        if abs(error) <= int(position_tolerance_raw):
            _hold_joint(bus, joint, reference_position_raw=int(last_snap["position_wrapped"]))
            return {
                "joint": joint,
                "target_present_raw": int(target_present_raw),
                "final_present_raw": int(last_snap["position"]),
                "final_present_wrapped_raw": int(last_snap["position_wrapped"]),
                "error_raw": int(error),
            }
        direction = 1 if error > 0 else -1
        cmd_step = min(abs(error), max(1, int(step_raw)))
        _command_goal_from_reference(bus, joint, direction, cmd_step, int(last_snap["position_wrapped"]))
        time.sleep(max(0.01, float(poll_interval_s)))
        last_snap = _read_joint_snapshot(bus, joint, tracker=tracker)
        if time.time() - start_ts > float(timeout_s):
            _hold_joint(bus, joint, reference_position_raw=int(last_snap["position_wrapped"]))
            raise TimeoutError(
                f"Timed out while returning {joint} to home reference. "
                f"target={target_present_raw}, last={last_snap['position']}"
            )


def _single_turn_zero_present_raw(max_res: int) -> int:
    return int(max_res / 2)


def _build_single_turn_calibration_entry(
    *,
    current_cal,
    max_res: int,
    homing_offset_raw: int,
    min_present_raw: int,
    max_present_raw: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    zero_present_raw = _single_turn_zero_present_raw(max_res)
    homing_offset_raw = int(homing_offset_raw)
    min_present_raw = int(min_present_raw)
    max_present_raw = int(max_present_raw)

    if not -2047 <= homing_offset_raw <= 2047:
        raise RuntimeError(
            f"Invalid single-turn homing offset for motor {current_cal.id}: "
            f"zero_raw={zero_present_raw}, homing_offset={homing_offset_raw}"
        )
    if not 0 <= min_present_raw < max_present_raw <= int(max_res):
        raise RuntimeError(
            f"Invalid single-turn range for motor {current_cal.id}: "
            f"recorded_range=[{min_present_raw}, {max_present_raw}]"
        )

    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(current_cal.drive_mode),
        "homing_offset": int(homing_offset_raw),
        "range_min": int(min_present_raw),
        "range_max": int(max_present_raw),
    }
    result_payload = {
        "calibration_mode": "single_turn_half_turn_zero",
        "homing_offset": int(homing_offset_raw),
        "zero_present_raw": int(zero_present_raw),
        "observed_range_min_raw": int(min_present_raw),
        "observed_range_max_raw": int(max_present_raw),
    }
    return entry, result_payload


def _build_multi_turn_calibration_entry(
    *,
    current_cal,
    home_present_raw: int,
    home_present_wrapped_raw: int,
    min_present_raw: int,
    max_present_raw: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    home_present_raw = int(home_present_raw)
    home_wrapped_raw = _wrap_position_raw(home_present_wrapped_raw)
    min_present_raw = int(min_present_raw)
    max_present_raw = int(max_present_raw)
    min_relative_raw = int(min_present_raw - home_present_raw)
    max_relative_raw = int(max_present_raw - home_present_raw)

    if min_relative_raw > 0 or max_relative_raw < 0:
        raise RuntimeError(
            "Invalid multi-turn calibration span: home is not inside observed range "
            f"(home={home_present_raw}, min={min_present_raw}, max={max_present_raw})"
        )
    if min_relative_raw >= max_relative_raw:
        raise RuntimeError(
            f"Invalid multi-turn calibration span: min_relative={min_relative_raw}, max_relative={max_relative_raw}"
        )

    entry = {
        "id": int(current_cal.id),
        "drive_mode": int(current_cal.drive_mode),
        "homing_offset": 0,
        "range_min": 0,
        "range_max": int(RAW_COUNTS_PER_REV - 1),
        "home_wrapped_raw": int(home_wrapped_raw),
        "home_tolerance_raw": int(DEFAULT_MULTI_TURN_HOME_TOLERANCE_RAW),
        "min_relative_raw": int(min_relative_raw),
        "max_relative_raw": int(max_relative_raw),
    }
    result_payload = {
        "calibration_mode": "multi_turn_mode0_relative_tracking",
        "home_present_raw": int(home_present_raw),
        "home_present_wrapped_raw": int(home_wrapped_raw),
        "home_tolerance_raw": int(DEFAULT_MULTI_TURN_HOME_TOLERANCE_RAW),
        "observed_range_min_raw": int(min_present_raw),
        "observed_range_max_raw": int(max_present_raw),
        "min_relative_raw": int(min_relative_raw),
        "max_relative_raw": int(max_relative_raw),
    }
    return entry, result_payload


def _calibrate(args: argparse.Namespace) -> Dict[str, Any]:
    config = resolve_config()
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else (SKILL_ROOT / "calibration" / f"{config.robot_id}.json").resolve()
    )
    joints = list(args.joints)

    with SoArmMoceController(config) as arm:
        bus = _ensure_bus_with_timeout(arm)
        try:
            current_hw_calib = bus.read_calibration()
            source_calib_path = (config.calib_dir / f"{config.robot_id}.json").resolve()
            source_calib_json = _read_json(source_calib_path)
            with bus.torque_disabled():
                for joint in joints:
                    _configure_joint_for_calibration(bus, joint)

            tracker: Dict[str, MultiTurnTrackerState] = {}
            home_present_raw: Dict[str, int] = {}
            home_present_wrapped_raw: Dict[str, int] = {}
            for joint in joints:
                snap = _read_joint_snapshot(bus, joint, tracker=tracker)
                home_present_raw[joint] = int(snap["position"])
                home_present_wrapped_raw[joint] = int(snap["position_wrapped"])

            results: Dict[str, Any] = {}
            min_present_raw: Dict[str, int] = {}
            max_present_raw: Dict[str, int] = {}
            single_turn_joints = [joint for joint in joints if joint not in MULTI_TURN_JOINTS]
            single_turn_homing_offsets: Dict[str, int] = {}
            seek_home_target_raw = dict(home_present_raw)

            if single_turn_joints:
                offsets = bus.set_half_turn_homings(single_turn_joints)
                single_turn_homing_offsets = {str(joint): int(offset) for joint, offset in offsets.items()}
                for joint in single_turn_joints:
                    model = bus.motors[joint].model
                    max_res = int(bus.model_resolution_table[model] - 1)
                    seek_home_target_raw[joint] = int(_single_turn_zero_present_raw(max_res))

            for joint in joints:
                step_raw = int(args.multi_turn_step_raw if joint in MULTI_TURN_JOINTS else args.single_turn_step_raw)
                neg_limit = _seek_limit(
                    bus=bus,
                    joint=joint,
                    direction=-1,
                    step_raw=step_raw,
                    poll_interval_s=args.poll_interval_s,
                    velocity_abs_threshold=args.velocity_abs_threshold,
                    movement_abs_threshold=args.movement_abs_threshold,
                    settle_samples=args.settle_samples,
                    stall_current_abs_threshold=args.stall_current_abs_threshold,
                    timeout_s=args.timeout_s,
                    tracker=tracker,
                )
                min_present_raw[joint] = int(neg_limit["limit_present_raw"])

                back_from_neg = _move_joint_back_to_target(
                    bus=bus,
                    joint=joint,
                    target_present_raw=int(seek_home_target_raw[joint]),
                    step_raw=step_raw,
                    poll_interval_s=args.poll_interval_s,
                    timeout_s=max(args.timeout_s, 2.0),
                    tracker=tracker,
                )

                pos_limit = _seek_limit(
                    bus=bus,
                    joint=joint,
                    direction=1,
                    step_raw=step_raw,
                    poll_interval_s=args.poll_interval_s,
                    velocity_abs_threshold=args.velocity_abs_threshold,
                    movement_abs_threshold=args.movement_abs_threshold,
                    settle_samples=args.settle_samples,
                    stall_current_abs_threshold=args.stall_current_abs_threshold,
                    timeout_s=args.timeout_s,
                    tracker=tracker,
                )
                max_present_raw[joint] = int(pos_limit["limit_present_raw"])

                back_from_pos = _move_joint_back_to_target(
                    bus=bus,
                    joint=joint,
                    target_present_raw=int(seek_home_target_raw[joint]),
                    step_raw=step_raw,
                    poll_interval_s=args.poll_interval_s,
                    timeout_s=max(args.timeout_s, 2.0),
                    tracker=tracker,
                )

                results[joint] = {
                    "negative_limit": neg_limit,
                    "return_from_negative": back_from_neg,
                    "positive_limit": pos_limit,
                    "return_from_positive": back_from_pos,
                    "home_present_raw": int(home_present_raw[joint]),
                    "home_present_wrapped_raw": int(home_present_wrapped_raw[joint]),
                }

            written_json = dict(source_calib_json) if isinstance(source_calib_json, dict) else {}
            register_writes: Dict[str, Dict[str, int]] = {}

            for joint in joints:
                current_cal = current_hw_calib[joint]
                if joint in MULTI_TURN_JOINTS:
                    entry, result_payload = _build_multi_turn_calibration_entry(
                        current_cal=current_cal,
                        home_present_raw=int(home_present_raw[joint]),
                        home_present_wrapped_raw=int(home_present_wrapped_raw[joint]),
                        min_present_raw=int(min_present_raw[joint]),
                        max_present_raw=int(max_present_raw[joint]),
                    )
                    written_json[joint] = entry
                    results[joint]["calibration"] = result_payload
                    register_writes[joint] = {
                        "homing_offset": int(entry["homing_offset"]),
                        "range_min": int(entry["range_min"]),
                        "range_max": int(entry["range_max"]),
                        "home_wrapped_raw": int(entry["home_wrapped_raw"]),
                        "min_relative_raw": int(entry["min_relative_raw"]),
                        "max_relative_raw": int(entry["max_relative_raw"]),
                        "calibration_mode": str(result_payload["calibration_mode"]),
                    }
                    continue

                model = bus.motors[joint].model
                max_res = int(bus.model_resolution_table[model] - 1)
                entry, result_payload = _build_single_turn_calibration_entry(
                    current_cal=current_cal,
                    max_res=max_res,
                    homing_offset_raw=int(single_turn_homing_offsets[joint]),
                    min_present_raw=int(min_present_raw[joint]),
                    max_present_raw=int(max_present_raw[joint]),
                )
                written_json[joint] = entry
                results[joint]["calibration"] = result_payload
                register_writes[joint] = {
                    "homing_offset": int(entry["homing_offset"]),
                    "range_min": int(entry["range_min"]),
                    "range_max": int(entry["range_max"]),
                    "zero_present_raw": int(result_payload["zero_present_raw"]),
                    "calibration_mode": str(result_payload["calibration_mode"]),
                }

            if args.apply_registers:
                for joint in joints:
                    write_spec = register_writes[joint]
                    bus.write("Homing_Offset", joint, int(write_spec["homing_offset"]), normalize=False)
                    bus.write("Min_Position_Limit", joint, int(write_spec["range_min"]), normalize=False)
                    bus.write("Max_Position_Limit", joint, int(write_spec["range_max"]), normalize=False)

            if args.save_json:
                written_json[CALIBRATION_META_KEY] = {
                    "home_joint_deg": {joint: 0.0 for joint in JOINTS},
                }
                _write_json(output_path, written_json)

            return {
                "action": "auto_calibrate",
                "robot_id": config.robot_id,
                "port": config.port,
                "source_calibration_path": str(source_calib_path),
                "output_path": str(output_path),
                "saved_json": bool(args.save_json),
                "applied_registers": bool(args.apply_registers),
                "home_reference_note": "place single-turn joints at the URDF q=0 pose before running; multi-turn joints use the current pose as software zero",
                "thresholds": {
                    "velocity_abs_threshold": float(args.velocity_abs_threshold),
                    "movement_abs_threshold": int(args.movement_abs_threshold),
                    "settle_samples": int(args.settle_samples),
                    "stall_current_abs_threshold": float(args.stall_current_abs_threshold),
                    "poll_interval_s": float(args.poll_interval_s),
                    "timeout_s": float(args.timeout_s),
                },
                "joints": joints,
                "results": results,
                "register_writes": register_writes,
            }
        except Exception:
            try:
                arm.stop()
            except Exception:
                pass
            raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Automatically calibrate soarmMoce from the current zero pose. "
            "Single-turn joints use URDF q=0; multi-turn joints use the current pose as software zero."
        )
    )
    parser.add_argument("--joints", type=_parse_joints, default=list(JOINTS), help="Comma-separated joints to calibrate")
    parser.add_argument("--output", default="", help="Output calibration JSON path")
    parser.add_argument(
        "--apply-registers",
        type=cli_bool,
        default=True,
        help="Whether to also write the computed calibration back to the motor registers (default: true)",
    )
    parser.add_argument(
        "--save-json",
        type=cli_bool,
        default=True,
        help="Whether to save the computed calibration JSON (default: true)",
    )
    parser.add_argument("--single-turn-step-raw", type=int, default=96, help="Raw step per seek iteration for single-turn joints")
    parser.add_argument("--multi-turn-step-raw", type=int, default=64, help="Raw step per seek iteration for multi-turn joints")
    parser.add_argument(
        "--velocity-abs-threshold",
        type=float,
        default=6.0,
        help="Primary limit-detection threshold on |Present_Velocity|",
    )
    parser.add_argument(
        "--movement-abs-threshold",
        type=int,
        default=8,
        help="Primary limit-detection threshold on recent |Present_Position| span",
    )
    parser.add_argument(
        "--settle-samples",
        type=int,
        default=4,
        help="How many recent samples must satisfy the primary/fallback condition",
    )
    parser.add_argument(
        "--stall-current-abs-threshold",
        type=float,
        default=350.0,
        help="Fallback threshold on |Present_Current| when velocity/moving do not settle cleanly",
    )
    parser.add_argument("--poll-interval-s", type=float, default=0.05, help="Polling interval during seek")
    parser.add_argument("--timeout-s", type=float, default=10.0, help="Timeout per seek direction")
    args = parser.parse_args()
    run_and_print(lambda: _calibrate(args))


if __name__ == "__main__":
    main()
