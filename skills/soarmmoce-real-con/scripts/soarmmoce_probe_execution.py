#!/usr/bin/env python3
"""Compare predicted IK target against actual execution for one small cartesian move."""

from __future__ import annotations

import argparse
from typing import Dict, Optional, Sequence

import numpy as np

from soarmmoce_sdk import ARM_JOINTS, LOCKED_CARTESIAN_JOINTS, IKTraceError, SoArmMoceController


def _xyz(vec: Sequence[float]) -> str:
    arr = np.asarray(vec, dtype=float)
    return f"({arr[0]:+.4f}, {arr[1]:+.4f}, {arr[2]:+.4f})"


def _joint_delta_summary(q_delta_deg: np.ndarray, threshold_deg: float = 0.05) -> str:
    parts = []
    for idx, name in enumerate(ARM_JOINTS):
        delta = float(q_delta_deg[idx])
        if abs(delta) < threshold_deg:
            continue
        parts.append(f"{name}={delta:+.2f}")
    return ", ".join(parts) if parts else "-"


def _joint_error_summary(
    q_target_deg: np.ndarray,
    q_actual_deg: np.ndarray,
    threshold_deg: float = 0.05,
) -> str:
    parts = []
    for idx, name in enumerate(ARM_JOINTS):
        err = float(q_actual_deg[idx] - q_target_deg[idx])
        if abs(err) < threshold_deg:
            continue
        parts.append(f"{name}={err:+.2f}")
    return ", ".join(parts) if parts else "-"


def _trace_last_step(trace: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if not isinstance(trace, dict):
        return None
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    last = steps[-1]
    return last if isinstance(last, dict) else None


def _trace_last_joint_target_deg(trace: Optional[Dict[str, object]]) -> Optional[np.ndarray]:
    last_step = _trace_last_step(trace)
    if not isinstance(last_step, dict):
        return None
    payload = last_step.get("target_joint_deg")
    if not isinstance(payload, dict):
        payload = last_step.get("ik_target_joint_deg")
    if not isinstance(payload, dict):
        return None
    return np.array([float(payload[name]) for name in ARM_JOINTS], dtype=float)


def _trace_last_ik_err_mm(trace: Optional[Dict[str, object]]) -> Optional[float]:
    last_step = _trace_last_step(trace)
    if not isinstance(last_step, dict):
        return None
    value = last_step.get("ik_pos_err_m")
    if not isinstance(value, (int, float)):
        return None
    return float(value) * 1000.0


def _max_joint_error_from_trace(trace: Optional[Dict[str, object]]) -> str:
    if not isinstance(trace, dict):
        return "-"
    summary = trace.get("summary")
    if not isinstance(summary, dict):
        return "-"
    payload = summary.get("max_abs_joint_error_deg")
    if not isinstance(payload, dict):
        return "-"
    items = sorted(((str(k), float(v)) for k, v in payload.items()), key=lambda item: abs(item[1]), reverse=True)
    shown = [f"{name}={value:.2f}" for name, value in items if abs(value) >= 0.05][:3]
    return ", ".join(shown) if shown else "-"


def _state_q_deg(state: Dict[str, object]) -> np.ndarray:
    return np.array([float(state["joint_state"][name]) for name in ARM_JOINTS], dtype=float)


def _state_xyz(state: Dict[str, object]) -> np.ndarray:
    return np.asarray(state["tcp_pose"]["xyz"], dtype=float)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe one cartesian command and compare predicted vs actual execution")
    parser.add_argument("--dx", type=float, default=0.0)
    parser.add_argument("--dy", type=float, default=0.0)
    parser.add_argument("--dz", type=float, default=0.0)
    parser.add_argument("--frame", choices=["base", "urdf", "user", "tool"], default="base")
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--return-to-start", action="store_true", help="Move back to the starting joint pose after the probe")
    args = parser.parse_args()

    request = np.array([float(args.dx), float(args.dy), float(args.dz)], dtype=float)
    if np.linalg.norm(request) < 1e-12:
        raise SystemExit("At least one of --dx/--dy/--dz must be non-zero")

    arm = SoArmMoceController()
    start_state = arm.get_state()
    start_q_deg = _state_q_deg(start_state)
    start_xyz = _state_xyz(start_state)
    current_tf = arm._forward_kinematics_from_arm_deg(start_q_deg)
    resolved_delta = arm._resolve_delta_in_model_frame(request, frame=str(args.frame), current_tf=current_tf)
    target_xyz = start_xyz + resolved_delta
    locked_joint_targets_deg = {
        joint_name: float(start_state["joint_state"][joint_name]) for joint_name in LOCKED_CARTESIAN_JOINTS
    }
    ik = arm._solve_ik_to_position(target_xyz, start_q_deg, locked_joint_targets_deg=locked_joint_targets_deg)
    q_target_deg = np.asarray(ik["q_target_deg"], dtype=float)
    predicted_q_delta_deg = q_target_deg - start_q_deg
    predicted_xyz = np.asarray(arm._forward_kinematics_from_arm_deg(q_target_deg).pos, dtype=float)
    predicted_err_m = float(np.linalg.norm(predicted_xyz - target_xyz))

    print(f"request={_xyz(request)} frame={args.frame} duration={float(args.duration):.2f}s")
    print(f"start_tcp={_xyz(start_xyz)} target_tcp={_xyz(target_xyz)}")
    print(
        f"predicted_err_mm={predicted_err_m * 1000:.2f} predicted_joint_delta={_joint_delta_summary(predicted_q_delta_deg)}"
    )

    result = None
    error: Optional[Exception] = None
    trace = None
    final_state = start_state
    try:
        result = arm.move_delta(
            dx=float(args.dx),
            dy=float(args.dy),
            dz=float(args.dz),
            frame=str(args.frame),
            duration=float(args.duration),
            wait=True,
            trace=True,
        )
        final_state = result["state"]
        trace = result.get("trace")
    except Exception as exc:
        error = exc
        if isinstance(exc, IKTraceError):
            details = exc.details or {}
            maybe_state = details.get("last_state")
            if isinstance(maybe_state, dict):
                final_state = maybe_state
            maybe_trace = details.get("trace")
            if isinstance(maybe_trace, dict):
                trace = maybe_trace
        else:
            raise
    finally:
        if args.return_to_start:
            try:
                arm.move_joints(
                    targets_deg={name: float(start_state["joint_state"][name]) for name in ARM_JOINTS},
                    duration=max(0.3, float(args.duration)),
                    wait=True,
                )
            except Exception:
                pass

    final_q_deg = _state_q_deg(final_state)
    final_xyz = _state_xyz(final_state)
    estimated_delta_from_fk = final_xyz - start_xyz
    final_err_m = float(np.linalg.norm(final_xyz - target_xyz))
    trace_last_target_deg = _trace_last_joint_target_deg(trace)
    trace_last_ik_err_mm = _trace_last_ik_err_mm(trace)
    status = "OK" if error is None else f"ERROR:{error.__class__.__name__}"

    print(
        f"final_tcp_estimated={_xyz(final_xyz)} "
        f"fk_delta_estimated={_xyz(estimated_delta_from_fk)} final_err_mm={final_err_m * 1000:.2f}"
    )
    print(f"one_shot_target_vs_final={_joint_error_summary(q_target_deg, final_q_deg)}")
    if trace_last_target_deg is not None:
        last_ik_suffix = ""
        if trace_last_ik_err_mm is not None:
            last_ik_suffix = f" last_waypoint_ik_err_mm={trace_last_ik_err_mm:.2f}"
        print(
            f"last_waypoint_target_vs_final={_joint_error_summary(trace_last_target_deg, final_q_deg)}"
            f"{last_ik_suffix}"
        )
    print(f"trace_max_joint_err={_max_joint_error_from_trace(trace)} status={status}")
    if error is not None:
        print(f"message={error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
