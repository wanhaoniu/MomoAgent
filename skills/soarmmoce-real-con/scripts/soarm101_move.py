#!/usr/bin/env python3
"""Arm motion entrypoints for soarmMoce."""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from soarm101_cli_common import cli_bool, print_error, print_success
from soarm101_sdk import SoArm101Controller


def main() -> None:
    parser = argparse.ArgumentParser(description="soarmMoce arm motion CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_delta = sub.add_parser("delta", help="Move TCP by relative dx/dy/dz")
    p_delta.add_argument("--dx", type=float, default=0.0)
    p_delta.add_argument("--dy", type=float, default=0.0)
    p_delta.add_argument("--dz", type=float, default=0.0)
    p_delta.add_argument("--frame", choices=["base", "tool"], default="base")
    p_delta.add_argument("--duration", type=float, default=1.0)
    p_delta.add_argument("--wait", type=cli_bool, default=True)
    p_delta.add_argument("--timeout", type=float, default=None)

    p_xyz = sub.add_parser("xyz", help="Move TCP to absolute x/y/z")
    p_xyz.add_argument("--x", type=float, default=None)
    p_xyz.add_argument("--y", type=float, default=None)
    p_xyz.add_argument("--z", type=float, default=None)
    p_xyz.add_argument("--duration", type=float, default=1.0)
    p_xyz.add_argument("--wait", type=cli_bool, default=True)
    p_xyz.add_argument("--timeout", type=float, default=None)

    p_joint = sub.add_parser("joint", help="Move one joint")
    p_joint.add_argument("--joint", required=True)
    p_joint.add_argument("--delta-deg", type=float, default=None)
    p_joint.add_argument("--target-deg", type=float, default=None)
    p_joint.add_argument("--duration", type=float, default=1.0)
    p_joint.add_argument("--wait", type=cli_bool, default=True)
    p_joint.add_argument("--timeout", type=float, default=None)

    p_joints = sub.add_parser("joints", help="Move multiple joints with JSON targets")
    p_joints.add_argument("--targets-json", required=True, help='JSON object, e.g. {"wrist_roll": 5}')
    p_joints.add_argument("--duration", type=float, default=1.0)
    p_joints.add_argument("--wait", type=cli_bool, default=True)
    p_joints.add_argument("--timeout", type=float, default=None)

    p_home = sub.add_parser("home", help="Move to configured home pose")
    p_home.add_argument("--duration", type=float, default=1.5)
    p_home.add_argument("--wait", type=cli_bool, default=True)
    p_home.add_argument("--timeout", type=float, default=None)

    sub.add_parser("stop", help="Hold current pose")

    args = parser.parse_args()

    try:
        arm = SoArm101Controller()
        if args.cmd == "delta":
            result = arm.move_delta(
                dx=args.dx,
                dy=args.dy,
                dz=args.dz,
                frame=args.frame,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
            )
        elif args.cmd == "xyz":
            result = arm.move_to(
                x=args.x,
                y=args.y,
                z=args.z,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
            )
        elif args.cmd == "joint":
            result = arm.move_joint(
                joint=args.joint,
                delta_deg=args.delta_deg,
                target_deg=args.target_deg,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
            )
        elif args.cmd == "joints":
            try:
                targets: Dict[str, Any] = json.loads(args.targets_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in --targets-json: {exc}") from exc
            result = arm.move_joints(
                targets_deg=targets,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
            )
        elif args.cmd == "home":
            result = arm.home(duration=args.duration, wait=args.wait, timeout=args.timeout)
        else:
            result = arm.stop()
        print_success(result)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
