#!/usr/bin/env python3
"""Probe actual cartesian motion directions with concise output."""

from __future__ import annotations

import argparse
from typing import Dict, List

import numpy as np

from soarmmoce_sdk import SoArmMoceController


AXIS_TO_VECTOR = {
    "dx": np.array([1.0, 0.0, 0.0], dtype=float),
    "dy": np.array([0.0, 1.0, 0.0], dtype=float),
    "dz": np.array([0.0, 0.0, 1.0], dtype=float),
}


def _parse_axes(raw: str) -> List[str]:
    items = [part.strip().lower() for part in str(raw or "").split(",")]
    axes = [item for item in items if item]
    if not axes:
        raise ValueError("--axes must contain at least one of dx,dy,dz")
    invalid = [item for item in axes if item not in AXIS_TO_VECTOR]
    if invalid:
        raise ValueError(f"Unknown axes: {', '.join(invalid)}")
    return axes


def _axis_label(index: int, sign: float) -> str:
    axis_name = "xyz"[index]
    return f"{'+' if sign >= 0.0 else '-'}{axis_name}"


def _tcp_xyz(state: Dict[str, object]) -> np.ndarray:
    return np.asarray(state["tcp_pose"]["xyz"], dtype=float)


def _joint_state(state: Dict[str, object]) -> Dict[str, float]:
    return {str(name): float(value) for name, value in dict(state["joint_state"]).items()}


def _one_probe(
    arm: SoArmMoceController,
    *,
    axis_name: str,
    step_m: float,
    frame: str,
    duration: float,
    settle_s: float,
    min_response_ratio: float,
    min_effective_motion_m: float,
    return_to_start: bool,
) -> Dict[str, object]:
    start_state = arm.get_state()
    start_xyz = _tcp_xyz(start_state)
    request = AXIS_TO_VECTOR[axis_name] * float(step_m)
    move_kwargs = {
        "dx": float(request[0]),
        "dy": float(request[1]),
        "dz": float(request[2]),
        "frame": frame,
        "duration": duration,
        "wait": True,
    }

    try:
        result = arm.move_delta(**move_kwargs)
        end_state = result["state"]
    finally:
        if return_to_start:
            try:
                arm.move_joints(
                    targets_deg=_joint_state(start_state),
                    duration=max(0.2, float(duration)),
                    wait=True,
                )
            except Exception:
                pass

    if settle_s > 0.0:
        import time

        time.sleep(float(settle_s))

    estimated_delta_from_fk = _tcp_xyz(end_state) - start_xyz
    requested_norm = float(np.linalg.norm(request))
    actual_norm = float(np.linalg.norm(estimated_delta_from_fk))
    dominant_idx = int(np.argmax(np.abs(estimated_delta_from_fk)))
    dominant_value = float(estimated_delta_from_fk[dominant_idx])
    expected_idx = int(np.argmax(np.abs(request)))
    expected_sign = float(np.sign(request[expected_idx]) or 1.0)
    match_axis = dominant_idx == expected_idx
    match_sign = float(np.sign(dominant_value) or 1.0) == expected_sign
    alignment = (
        0.0
        if requested_norm < 1e-12 or actual_norm < 1e-12
        else float(np.dot(estimated_delta_from_fk, request) / (requested_norm * actual_norm))
    )
    response_ratio = 0.0 if requested_norm < 1e-12 else float(actual_norm / requested_norm)
    enough_motion = actual_norm >= max(float(min_effective_motion_m), float(min_response_ratio) * requested_norm)
    if not enough_motion:
        status = "weak_motion"
    elif match_axis and match_sign:
        status = "ok"
    else:
        status = "mismatch"
    return {
        "axis": axis_name,
        "request_m": request.tolist(),
        "estimated_fk_delta_m": estimated_delta_from_fk.tolist(),
        "actual_norm_m": actual_norm,
        "response_ratio": response_ratio,
        "dominant": _axis_label(dominant_idx, dominant_value),
        "expected": _axis_label(expected_idx, expected_sign),
        "match": bool(match_axis and match_sign),
        "alignment": alignment,
        "status": status,
    }


def _print_probe(result: Dict[str, object]) -> None:
    actual = np.asarray(result["estimated_fk_delta_m"], dtype=float)
    print(
        f"{result['axis']}: fk_delta=({actual[0]:+.4f}, {actual[1]:+.4f}, {actual[2]:+.4f}) "
        f"dominant={result['dominant']} expected={result['expected']} "
        f"match={'yes' if result['match'] else 'no'} align={float(result['alignment']):+.3f} "
        f"resp={float(result['response_ratio']):.2f} status={result['status']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check actual cartesian motion axes with small real-arm probes")
    parser.add_argument("--axes", default="dx,dy,dz", help="Comma-separated subset of dx,dy,dz")
    parser.add_argument("--step-m", type=float, default=0.01, help="Probe step in meters")
    parser.add_argument("--frame", choices=["base", "urdf", "user", "tool"], default="base")
    parser.add_argument("--duration", type=float, default=0.8, help="Move duration for each probe")
    parser.add_argument("--settle-s", type=float, default=0.1, help="Extra wait after each probe before next axis")
    parser.add_argument(
        "--min-response-ratio",
        type=float,
        default=0.3,
        help="Mark result as weak_motion when actual displacement is below this fraction of requested step",
    )
    parser.add_argument(
        "--min-effective-motion-m",
        type=float,
        default=0.002,
        help="Mark result as weak_motion when actual displacement is below this absolute threshold",
    )
    parser.add_argument(
        "--no-return",
        action="store_true",
        help="Do not automatically move back to the start pose after each probe",
    )
    args = parser.parse_args()

    axes = _parse_axes(args.axes)
    print(f"frame={args.frame} step_m={float(args.step_m):.4f} axes={','.join(axes)}")
    arm = SoArmMoceController()
    for axis_name in axes:
        result = _one_probe(
            arm,
            axis_name=axis_name,
            step_m=float(args.step_m),
            frame=str(args.frame),
            duration=float(args.duration),
            settle_s=float(args.settle_s),
            min_response_ratio=float(args.min_response_ratio),
            min_effective_motion_m=float(args.min_effective_motion_m),
            return_to_start=not bool(args.no_return),
        )
        _print_probe(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
