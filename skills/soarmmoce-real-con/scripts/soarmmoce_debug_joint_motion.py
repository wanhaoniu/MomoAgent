#!/usr/bin/env python3
"""Run one joint move with detailed before/after logging for jump diagnosis."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import JOINTS, MULTI_TURN_JOINTS, SoArmMoceController, ValidationError, to_jsonable


def _snapshot(arm: SoArmMoceController) -> Dict[str, Any]:
    bus = arm._ensure_bus()
    state = arm.get_state()
    raw_present = arm._read_raw_present_position(bus)
    multi_turn_state = arm._snapshot_multi_turn_state()
    return {
        "timestamp": time.time(),
        "joint_state": to_jsonable(state.get("joint_state")),
        "tcp_pose": to_jsonable(state.get("tcp_pose")),
        "raw_present_position": to_jsonable(raw_present),
        "multi_turn_state": to_jsonable(multi_turn_state),
        "last_multi_turn_goal_raw_mod": to_jsonable(dict(arm._last_multi_turn_goal_raw_mod)),
        "last_multi_turn_goal_continuous_raw": to_jsonable(dict(arm._last_multi_turn_goal_continuous_raw)),
        "last_multi_turn_goal_joint_deg": to_jsonable(dict(arm._last_multi_turn_goal_joint_deg)),
    }


def _sample_post_motion(
    arm: SoArmMoceController,
    *,
    joint_name: str,
    observe_sec: float,
    sample_hz: float,
    target_deg: float,
) -> Dict[str, Any]:
    observe_sec = max(0.0, float(observe_sec))
    sample_hz = max(1.0, float(sample_hz))
    dt = 1.0 / sample_hz
    deadline = time.monotonic() + observe_sec
    samples = []
    max_abs_error = 0.0
    peak_to_peak = 0.0
    values = []

    while True:
        snap = _snapshot(arm)
        joint_state = snap.get("joint_state") or {}
        joint_value = float(joint_state.get(joint_name, 0.0))
        error_deg = joint_value - float(target_deg)
        snap["observed_joint"] = {
            "name": joint_name,
            "value_deg": joint_value,
            "target_deg": float(target_deg),
            "error_deg": float(error_deg),
        }
        samples.append(snap)
        values.append(joint_value)
        max_abs_error = max(max_abs_error, abs(error_deg))
        peak_to_peak = max(peak_to_peak, max(values) - min(values))
        if time.monotonic() >= deadline:
            break
        time.sleep(dt)

    return {
        "observe_sec": observe_sec,
        "sample_hz": sample_hz,
        "sample_count": len(samples),
        "max_abs_error_deg": float(max_abs_error),
        "peak_to_peak_deg": float(peak_to_peak),
        "samples": samples,
    }


def _default_output_path(joint_name: str, runtime_dir: Path) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return runtime_dir / f"joint_motion_debug_{joint_name}_{stamp}.json"


def _read_file_from_offset(path: Path, offset: int) -> str:
    try:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, int(offset)))
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def run_debug(
    *,
    joint_name: str,
    target_deg: float | None,
    delta_deg: float | None,
    duration: float,
    observe_sec: float,
    sample_hz: float,
    output_path: Path | None,
) -> Dict[str, Any]:
    if joint_name not in JOINTS:
        raise ValidationError(f"Unknown joint: {joint_name}")
    if (target_deg is None) == (delta_deg is None):
        raise ValidationError("Exactly one of --target-deg or --delta-deg must be provided")

    arm = SoArmMoceController()
    try:
        if output_path is None:
            output_path = _default_output_path(joint_name, arm.config.runtime_dir)
        sdk_log_path = arm.config.runtime_dir / "sdk_multi_turn_debug.log"
        sdk_log_start_offset = sdk_log_path.stat().st_size if sdk_log_path.exists() else 0
        before = _snapshot(arm)
        move_result = arm.move_joint(
            joint=joint_name,
            target_deg=target_deg,
            delta_deg=delta_deg,
            duration=float(duration),
            wait=True,
            trace=True,
        )
        commanded_target_deg = float(move_result["target_deg"])
        post_observe = _sample_post_motion(
            arm,
            joint_name=joint_name,
            observe_sec=float(observe_sec),
            sample_hz=float(sample_hz),
            target_deg=commanded_target_deg,
        )
        payload = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "joint": joint_name,
            "command": {
                "target_deg": None if target_deg is None else float(target_deg),
                "delta_deg": None if delta_deg is None else float(delta_deg),
                "duration": float(duration),
            },
            "before": before,
            "move_result": to_jsonable(move_result),
            "post_observe": post_observe,
            "sdk_debug_log": {
                "path": str(sdk_log_path),
                "captured_from_offset": int(sdk_log_start_offset),
                "new_text": _read_file_from_offset(sdk_log_path, sdk_log_start_offset),
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        final_joint = float(move_result["state"]["joint_state"][joint_name])
        summary = {
            "action": "debug_joint_motion",
            "joint": joint_name,
            "multi_turn_joint": joint_name in MULTI_TURN_JOINTS,
            "target_deg": commanded_target_deg,
            "final_deg": final_joint,
            "final_error_deg": float(final_joint - commanded_target_deg),
            "post_observe_max_abs_error_deg": float(post_observe["max_abs_error_deg"]),
            "post_observe_peak_to_peak_deg": float(post_observe["peak_to_peak_deg"]),
            "saved_log_path": str(output_path),
            "sdk_log_path": str(sdk_log_path),
        }
        return summary
    finally:
        arm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug one joint move and save detailed motion logs")
    parser.add_argument("--joint", default="elbow_flex")
    parser.add_argument("--target-deg", type=float, default=None)
    parser.add_argument("--delta-deg", type=float, default=None)
    parser.add_argument("--duration", type=float, default=1.5)
    parser.add_argument("--observe-sec", type=float, default=2.0)
    parser.add_argument("--sample-hz", type=float, default=40.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    try:
        print_success(
            run_debug(
                joint_name=str(args.joint).strip(),
                target_deg=args.target_deg,
                delta_deg=args.delta_deg,
                duration=float(args.duration),
                observe_sec=float(args.observe_sec),
                sample_hz=float(args.sample_hz),
                output_path=Path(args.output).expanduser() if args.output else None,
            )
        )
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
