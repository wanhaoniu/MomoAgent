#!/usr/bin/env python3
"""Move one joint back to the home pose recorded in calibration JSON."""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import MULTI_TURN_JOINTS, SoArmMoceController, ValidationError, resolve_config


def _load_calibration_entry(config, joint: str) -> tuple[Dict[str, Any], str]:
    calib_path = config.calib_dir / f"{config.robot_id}.json"
    payload = json.loads(calib_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValidationError(f"Calibration JSON must be an object: {calib_path}")
    entry = payload.get(joint)
    if not isinstance(entry, dict):
        raise ValidationError(f"Calibration entry for {joint} not found in {calib_path}")
    return entry, str(calib_path)


def _nearest_continuous_raw(current_continuous_raw: float, wrapped_raw: int) -> tuple[int, int]:
    wrapped = int(wrapped_raw) % 4096
    turn_index = int(round((float(current_continuous_raw) - float(wrapped)) / 4096.0))
    return int(wrapped + turn_index * 4096), int(turn_index)


def move_joint_to_calibrated_home(*, joint: str, duration: float, timeout: float | None, trace: bool) -> Dict[str, Any]:
    arm = SoArmMoceController()
    config = resolve_config()
    entry, calib_path = _load_calibration_entry(config, joint)

    if joint not in MULTI_TURN_JOINTS:
        raise ValidationError(
            f"{joint} is not a multi-turn joint. This test script is only for {', '.join(MULTI_TURN_JOINTS)}."
        )
    if "home_wrapped_raw" not in entry:
        raise ValidationError(
            f"Calibration entry for {joint} does not contain home_wrapped_raw. Re-run manual calibration first."
        )

    before_state = arm.get_state()
    snapshot = arm._snapshot_multi_turn_state()
    if joint not in snapshot:
        raise RuntimeError(f"Multi-turn runtime state for {joint} is unavailable after reading the arm state")

    current_joint_deg = float(before_state["joint_state"][joint])
    current_continuous_raw = float(snapshot[joint]["continuous_raw"])
    home_wrapped_raw = int(entry["home_wrapped_raw"])
    target_continuous_raw, turn_index = _nearest_continuous_raw(current_continuous_raw, home_wrapped_raw)
    target_motor_deg = float(target_continuous_raw) * 360.0 / 4096.0
    target_joint_deg = float(arm._motor_to_joint_deg(joint, target_motor_deg))

    result = arm.move_joint(
        joint=joint,
        target_deg=target_joint_deg,
        duration=float(duration),
        wait=True,
        timeout=timeout,
        trace=trace,
    )

    after_state = result["state"]
    final_snapshot = arm._snapshot_multi_turn_state()
    final_continuous_raw = float(final_snapshot.get(joint, {}).get("continuous_raw", target_continuous_raw))
    final_wrapped_raw = int(round(final_continuous_raw)) % 4096

    return {
        "action": "move_joint_to_calibrated_home",
        "joint": joint,
        "calibration_path": calib_path,
        "home_wrapped_raw": int(home_wrapped_raw) % 4096,
        "selected_turn_index": int(turn_index),
        "target_continuous_raw": int(round(target_continuous_raw)),
        "before_joint_deg": current_joint_deg,
        "target_joint_deg": target_joint_deg,
        "final_joint_deg": float(after_state["joint_state"][joint]),
        "final_continuous_raw": int(round(final_continuous_raw)),
        "final_wrapped_raw": int(final_wrapped_raw),
        "wrapped_raw_error": int(final_wrapped_raw - (int(home_wrapped_raw) % 4096)),
        "trace": result.get("trace") if trace else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Move one multi-turn joint to the home recorded in calibration JSON")
    parser.add_argument("--joint", required=True, choices=list(MULTI_TURN_JOINTS))
    parser.add_argument("--duration", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()

    try:
        print_success(
            move_joint_to_calibrated_home(
                joint=str(args.joint),
                duration=float(args.duration),
                timeout=None if args.timeout is None else float(args.timeout),
                trace=bool(args.trace),
            )
        )
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
