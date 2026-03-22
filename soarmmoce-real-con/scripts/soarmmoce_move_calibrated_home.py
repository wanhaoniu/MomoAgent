#!/usr/bin/env python3
"""Move one multi-turn joint back to the calibrated home pose."""

from __future__ import annotations

import argparse
from typing import Any, Dict

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import MULTI_TURN_JOINTS, SoArmMoceController, ValidationError


def move_joint_to_calibrated_home(*, joint: str, duration: float, timeout: float | None, trace: bool) -> Dict[str, Any]:
    arm = SoArmMoceController()

    if joint not in MULTI_TURN_JOINTS:
        raise ValidationError(
            f"{joint} is not a multi-turn joint. This test script is only for {', '.join(MULTI_TURN_JOINTS)}."
        )

    before_state = arm.get_state()
    current_joint_deg = float(before_state["joint_state"][joint])
    target_joint_deg = 0.0

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
    final_continuous_raw = float(final_snapshot.get(joint, {}).get("continuous_raw", 0.0))

    return {
        "action": "move_joint_to_calibrated_home",
        "joint": joint,
        "target_joint_deg": 0.0,
        "before_joint_deg": current_joint_deg,
        "final_joint_deg": float(after_state["joint_state"][joint]),
        "final_continuous_raw": int(round(final_continuous_raw)),
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
