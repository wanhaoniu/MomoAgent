#!/usr/bin/env python3
"""Read-only XYZ IK sweep around the current TCP pose."""

from __future__ import annotations

import argparse
from typing import Iterable, List, Sequence

import numpy as np

from soarmmoce_sdk import ARM_JOINTS, LOCKED_CARTESIAN_JOINTS, SoArmMoceController


def _format_xyz(vec: Sequence[float]) -> str:
    arr = np.asarray(vec, dtype=float)
    return f"({arr[0]:+.4f}, {arr[1]:+.4f}, {arr[2]:+.4f})"


def _format_joint_delta(q_delta_deg: np.ndarray) -> str:
    parts = []
    for idx, name in enumerate(ARM_JOINTS):
        delta = float(q_delta_deg[idx])
        if abs(delta) < 0.05:
            continue
        parts.append(f"{name}={delta:+.2f}")
    return ", ".join(parts) if parts else "-"


def _default_offsets(step_m: float, include_diagonals: bool) -> List[tuple[str, np.ndarray]]:
    step = float(step_m)
    offsets = [
        ("center", np.array([0.0, 0.0, 0.0], dtype=float)),
        ("+x", np.array([step, 0.0, 0.0], dtype=float)),
        ("-x", np.array([-step, 0.0, 0.0], dtype=float)),
        ("+y", np.array([0.0, step, 0.0], dtype=float)),
        ("-y", np.array([0.0, -step, 0.0], dtype=float)),
        ("+z", np.array([0.0, 0.0, step], dtype=float)),
        ("-z", np.array([0.0, 0.0, -step], dtype=float)),
    ]
    if include_diagonals:
        offsets.extend(
            [
                ("+x+z", np.array([step, 0.0, step], dtype=float)),
                ("-x+z", np.array([-step, 0.0, step], dtype=float)),
                ("+y+z", np.array([0.0, step, step], dtype=float)),
                ("-y+z", np.array([0.0, -step, step], dtype=float)),
            ]
        )
    return offsets


def _iter_cases(step_m: float, include_diagonals: bool) -> Iterable[tuple[str, np.ndarray]]:
    return _default_offsets(step_m=step_m, include_diagonals=include_diagonals)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only XYZ IK sweep around the current TCP pose")
    parser.add_argument("--step-m", type=float, default=0.01, help="Offset magnitude for each axis probe")
    parser.add_argument("--include-diagonals", action="store_true", help="Also test a few small diagonal offsets")
    parser.add_argument(
        "--limit-m",
        type=float,
        default=None,
        help="Override acceptable IK error in meters; defaults to the SDK max_ee_pos_err_m",
    )
    args = parser.parse_args()

    arm = SoArmMoceController()
    state = arm.get_state()
    q_seed_deg = np.array([float(state["joint_state"][name]) for name in ARM_JOINTS], dtype=float)
    current_xyz = np.asarray(state["tcp_pose"]["xyz"], dtype=float)
    error_limit_m = float(args.limit_m) if args.limit_m is not None else float(arm.config.max_ee_pos_err_m)
    locked_joint_targets_deg = {
        joint_name: float(state["joint_state"][joint_name]) for joint_name in LOCKED_CARTESIAN_JOINTS
    }

    print(f"seed_tcp={_format_xyz(current_xyz)} step_m={float(args.step_m):.4f} limit_m={error_limit_m:.4f}")
    print(
        "case   target_xyz                  err_mm  status  max_dq_deg  joint_delta"
    )
    print(
        "-----  --------------------------  ------  ------  ----------  ----------------------------------------------"
    )

    worst_ok_err = 0.0
    worst_ok_label = "center"
    failures = 0
    for label, offset in _iter_cases(step_m=float(args.step_m), include_diagonals=bool(args.include_diagonals)):
        target_xyz = current_xyz + offset
        try:
            ik = arm._solve_ik_to_position(
                target_xyz,
                q_seed_deg,
                locked_joint_targets_deg=locked_joint_targets_deg,
            )
            q_target_deg = np.asarray(ik["q_target_deg"], dtype=float)
            achieved_xyz = np.asarray(arm._forward_kinematics_from_arm_deg(q_target_deg).pos, dtype=float)
            err_m = float(np.linalg.norm(achieved_xyz - target_xyz))
            q_delta_deg = q_target_deg - q_seed_deg
            max_dq_deg = float(np.max(np.abs(q_delta_deg)))
            status = "OK" if err_m <= error_limit_m else "HIGH"
            if err_m <= error_limit_m and err_m >= worst_ok_err:
                worst_ok_err = err_m
                worst_ok_label = label
            if status != "OK":
                failures += 1
            print(
                f"{label:<5}  {_format_xyz(target_xyz):<26}  {err_m * 1000:6.2f}  "
                f"{status:<6}  {max_dq_deg:10.2f}  {_format_joint_delta(q_delta_deg)}"
            )
        except Exception as exc:
            failures += 1
            print(f"{label:<5}  {_format_xyz(target_xyz):<26}  {'-':>6}  ERROR   {'-':>10}  {exc}")

    print(
        f"summary: failures={failures} worst_ok={worst_ok_label} worst_ok_err_mm={worst_ok_err * 1000:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
