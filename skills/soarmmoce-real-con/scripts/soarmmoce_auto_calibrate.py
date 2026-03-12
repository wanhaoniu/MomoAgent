#!/usr/bin/env python3
"""Automatic calibration for the 5-servo soarmMoce real arm."""

from __future__ import annotations

import argparse
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict

from soarmmoce_cli_common import cli_bool, run_and_print
from soarmmoce_sdk import (
    JOINTS,
    MULTI_TURN_JOINTS,
    SKILL_ROOT,
    SoArmMoceController,
    resolve_config,
)


RAW_COUNTS_PER_REV = 4096
HALF_RAW_COUNTS_PER_REV = RAW_COUNTS_PER_REV / 2.0


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


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _unwrap_position_raw(joint: str, wrapped_raw: int, tracker: Dict[str, Dict[str, float]] | None) -> int:
    wrapped = int(wrapped_raw) % RAW_COUNTS_PER_REV
    if tracker is None or joint not in MULTI_TURN_JOINTS:
        return int(wrapped_raw)
    state = tracker.get(joint)
    if state is None:
        continuous = float(wrapped)
    else:
        last_wrapped = float(state["last_wrapped_raw"])
        last_continuous = float(state["continuous_raw"])
        delta = float(wrapped) - last_wrapped
        if delta > HALF_RAW_COUNTS_PER_REV:
            delta -= RAW_COUNTS_PER_REV
        elif delta < -HALF_RAW_COUNTS_PER_REV:
            delta += RAW_COUNTS_PER_REV
        continuous = last_continuous + delta
    tracker[joint] = {
        "last_wrapped_raw": float(wrapped),
        "continuous_raw": float(continuous),
    }
    return int(round(continuous))


def _read_joint_snapshot(bus, joint: str, tracker: Dict[str, Dict[str, float]] | None = None) -> Dict[str, Any]:
    wrapped_raw = int(bus.read("Present_Position", joint, normalize=False))
    position_key = _unwrap_position_raw(joint, wrapped_raw, tracker)
    return {
        "position": int(position_key),
        "position_wrapped": int(wrapped_raw),
        "velocity": float(bus.read("Present_Velocity", joint, normalize=False)),
        "moving": int(bus.read("Moving", joint, normalize=False)),
        "current": float(bus.read("Present_Current", joint, normalize=False)),
    }


def _command_goal_from_reference(bus, joint: str, direction: int, step_raw: int, reference_position_raw: int) -> Dict[str, Any]:
    direction = 1 if direction >= 0 else -1
    step_raw = max(1, int(step_raw))
    if joint in MULTI_TURN_JOINTS:
        goal_value = int(direction * step_raw)
    else:
        goal_value = int(min(4095, max(0, int(reference_position_raw) + direction * step_raw)))
    bus.write("Goal_Position", joint, goal_value, normalize=False)
    return {
        "kind": "incremental_step" if joint in MULTI_TURN_JOINTS else "absolute_goal",
        "goal_value": int(goal_value),
        "from_position": int(reference_position_raw),
    }


def _hold_joint(bus, joint: str, reference_position_raw: int | None = None) -> None:
    if joint in MULTI_TURN_JOINTS:
        bus.write("Goal_Position", joint, 0, normalize=False)
        return
    if reference_position_raw is None:
        reference_position_raw = int(bus.read("Present_Position", joint, normalize=False))
    bus.write("Goal_Position", joint, int(reference_position_raw), normalize=False)


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
    tracker: Dict[str, Dict[str, float]] | None,
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
    tracker: Dict[str, Dict[str, float]] | None,
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
    tracker: Dict[str, Dict[str, float]] | None = None,
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


def _desired_home_present_raw(max_res: int, motor_home_deg: float) -> int:
    half_turn = int(max_res / 2)
    return int(round(half_turn + float(motor_home_deg) * float(max_res) / 360.0))


def _calibrate(args: argparse.Namespace) -> Dict[str, Any]:
    config = resolve_config()
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else (SKILL_ROOT / "calibration" / f"{config.robot_id}.json").resolve()
    )
    joints = list(args.joints)

    with SoArmMoceController(config) as arm:
        bus = arm._ensure_bus()
        try:
            current_hw_calib = bus.read_calibration()
            source_calib_path = (config.calib_dir / f"{config.robot_id}.json").resolve()
            source_calib_json = _read_json(source_calib_path)
            tracker: Dict[str, Dict[str, float]] = {}
            home_present_raw: Dict[str, int] = {}
            home_present_wrapped_raw: Dict[str, int] = {}
            for joint in joints:
                snap = _read_joint_snapshot(bus, joint, tracker=tracker)
                home_present_raw[joint] = int(snap["position"])
                home_present_wrapped_raw[joint] = int(snap["position_wrapped"])

            for joint in joints:
                if joint in MULTI_TURN_JOINTS:
                    continue
                model = bus.motors[joint].model
                max_res = int(bus.model_resolution_table[model] - 1)
                bus.write("Min_Position_Limit", joint, 0, normalize=False)
                bus.write("Max_Position_Limit", joint, max_res, normalize=False)

            results: Dict[str, Any] = {}
            min_present_raw: Dict[str, int] = {}
            max_present_raw: Dict[str, int] = {}

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
                    target_present_raw=int(home_present_raw[joint]),
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
                    target_present_raw=int(home_present_raw[joint]),
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
                model = bus.motors[joint].model
                max_res = int(bus.model_resolution_table[model] - 1)
                motor_home_deg = arm._joint_to_motor_deg(joint, float(config.home_joints.get(joint, 0.0)))
                desired_home = _desired_home_present_raw(max_res, motor_home_deg)
                new_offset = int(round(int(current_cal.homing_offset) + int(home_present_raw[joint]) - desired_home))
                new_min = int(round(int(min_present_raw[joint]) - int(home_present_raw[joint]) + desired_home))
                new_max = int(round(int(max_present_raw[joint]) - int(home_present_raw[joint]) + desired_home))
                if new_min >= new_max:
                    raise RuntimeError(f"Invalid calibration span for {joint}: min={new_min}, max={new_max}")

                written_json[joint] = {
                    "id": int(current_cal.id),
                    "drive_mode": int(current_cal.drive_mode),
                    "homing_offset": int(new_offset),
                    "range_min": int(new_min),
                    "range_max": int(new_max),
                }
                register_writes[joint] = {
                    "homing_offset": int(new_offset),
                    "range_min": int(new_min),
                    "range_max": int(new_max),
                    "desired_home_present_raw": int(desired_home),
                }

            if args.apply_registers:
                for joint in joints:
                    write_spec = register_writes[joint]
                    bus.write("Homing_Offset", joint, int(write_spec["homing_offset"]), normalize=False)
                    if joint not in MULTI_TURN_JOINTS:
                        bus.write("Min_Position_Limit", joint, int(write_spec["range_min"]), normalize=False)
                        bus.write("Max_Position_Limit", joint, int(write_spec["range_max"]), normalize=False)

            if args.save_json:
                _write_json(output_path, written_json)

            return {
                "action": "auto_calibrate",
                "robot_id": config.robot_id,
                "port": config.port,
                "source_calibration_path": str(source_calib_path),
                "output_path": str(output_path),
                "saved_json": bool(args.save_json),
                "applied_registers": bool(args.apply_registers),
                "home_reference_note": "run this script with the arm already placed at the desired home pose",
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
            "Automatically calibrate soarmMoce from the current home pose. "
            "Place the arm at the desired home pose before running."
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
