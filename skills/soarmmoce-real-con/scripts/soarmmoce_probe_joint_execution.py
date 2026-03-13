#!/usr/bin/env python3
"""Probe one direct joint command and compare target vs actual execution."""

from __future__ import annotations

import argparse
from typing import Sequence

import numpy as np

from soarmmoce_sdk import JOINTS, IKTraceError, SoArmMoceController


def _xyz(vec: Sequence[float]) -> str:
    arr = np.asarray(vec, dtype=float)
    return f"({arr[0]:+.4f}, {arr[1]:+.4f}, {arr[2]:+.4f})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe one joint command and compare target vs actual execution")
    parser.add_argument("--joint", choices=JOINTS, required=True)
    parser.add_argument("--delta-deg", type=float, required=True)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--return-to-start", action="store_true")
    args = parser.parse_args()

    arm = SoArmMoceController()
    start_state = arm.get_state()
    start_q = {name: float(start_state["joint_state"][name]) for name in JOINTS}
    start_xyz = np.asarray(start_state["tcp_pose"]["xyz"], dtype=float)
    target_deg = float(start_q[args.joint] + float(args.delta_deg))

    print(
        f"joint={args.joint} start_deg={start_q[args.joint]:+.2f} "
        f"target_deg={target_deg:+.2f} delta_deg={float(args.delta_deg):+.2f}"
    )

    result = None
    error = None
    trace = None
    final_state = start_state
    try:
        result = arm.move_joint(
            joint=str(args.joint),
            delta_deg=float(args.delta_deg),
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
                arm.move_joints(targets_deg=start_q, duration=max(0.3, float(args.duration)), wait=True)
            except Exception:
                pass

    final_deg = float(final_state["joint_state"][args.joint])
    final_xyz = np.asarray(final_state["tcp_pose"]["xyz"], dtype=float)
    actual_delta_deg = final_deg - float(start_q[args.joint])
    joint_err_deg = final_deg - target_deg
    tcp_delta = final_xyz - start_xyz

    max_joint_err = "-"
    if isinstance(trace, dict):
        summary = trace.get("summary")
        if isinstance(summary, dict):
            payload = summary.get("max_abs_joint_error_deg")
            if isinstance(payload, dict) and args.joint in payload:
                max_joint_err = f"{float(payload[args.joint]):.2f}"

    status = "OK" if error is None else f"ERROR:{error.__class__.__name__}"
    print(
        f"final_deg={final_deg:+.2f} actual_delta_deg={actual_delta_deg:+.2f} "
        f"joint_err_deg={joint_err_deg:+.2f}"
    )
    print(f"tcp_delta={_xyz(tcp_delta)} trace_max_joint_err={max_joint_err} status={status}")
    if error is not None:
        print(f"message={error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
