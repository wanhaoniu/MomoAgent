from __future__ import annotations

import argparse
import json
from typing import Any

from ..cli_common import run_and_print
from ..real_arm import SoArmMoceController, ValidationError


def _parse_targets_json(raw: str) -> dict[str, float]:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON for --targets: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("--targets must be a JSON object mapping joint names to degree values")
    return {str(joint): float(value) for joint, value in payload.items()}


def _run_home(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.home(duration=float(args.duration), wait=bool(args.wait))


def _run_stop(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.stop()


def _run_joint(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.move_joint(
            joint=str(args.joint),
            target_deg=args.target_deg,
            delta_deg=args.delta_deg,
            duration=float(args.duration),
            wait=bool(args.wait),
            trace=bool(args.trace),
        )


def _run_joints(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.move_joints(
            _parse_targets_json(args.targets),
            duration=float(args.duration),
            wait=bool(args.wait),
            trace=bool(args.trace),
        )


def _run_delta(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.move_delta(
            dx=float(args.dx),
            dy=float(args.dy),
            dz=float(args.dz),
            frame=str(args.frame),
            duration=float(args.duration),
            wait=bool(args.wait),
        )


def _run_xyz(args: argparse.Namespace) -> dict[str, Any]:
    with SoArmMoceController() as arm:
        return arm.move_pose(
            xyz=[float(args.x), float(args.y), float(args.z)],
            rpy=None,
            duration=float(args.duration),
            wait=bool(args.wait),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="soarmmoce motion CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    home_parser = subparsers.add_parser("home", help="Return to the startup pose")
    home_parser.add_argument("--duration", type=float, default=1.0)
    home_parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    home_parser.set_defaults(_handler=_run_home)

    stop_parser = subparsers.add_parser("stop", help="Hold the current raw pose")
    stop_parser.set_defaults(_handler=_run_stop)

    joint_parser = subparsers.add_parser("joint", help="Move one joint in degrees")
    joint_parser.add_argument("--joint", required=True)
    joint_parser.add_argument("--target-deg", type=float, default=None)
    joint_parser.add_argument("--delta-deg", type=float, default=None)
    joint_parser.add_argument("--duration", type=float, default=1.0)
    joint_parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    joint_parser.add_argument("--trace", action=argparse.BooleanOptionalAction, default=False)
    joint_parser.set_defaults(_handler=_run_joint)

    joints_parser = subparsers.add_parser("joints", help="Move multiple joints with a JSON map")
    joints_parser.add_argument("--targets", required=True, help='JSON object, e.g. {"shoulder_pan": 10}')
    joints_parser.add_argument("--duration", type=float, default=1.0)
    joints_parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    joints_parser.add_argument("--trace", action=argparse.BooleanOptionalAction, default=False)
    joints_parser.set_defaults(_handler=_run_joints)

    delta_parser = subparsers.add_parser("delta", help="Cartesian delta move")
    delta_parser.add_argument("--dx", type=float, default=0.0)
    delta_parser.add_argument("--dy", type=float, default=0.0)
    delta_parser.add_argument("--dz", type=float, default=0.0)
    delta_parser.add_argument("--frame", default="base")
    delta_parser.add_argument("--duration", type=float, default=1.0)
    delta_parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    delta_parser.set_defaults(_handler=_run_delta)

    xyz_parser = subparsers.add_parser("xyz", help="Cartesian absolute move")
    xyz_parser.add_argument("--x", type=float, required=True)
    xyz_parser.add_argument("--y", type=float, required=True)
    xyz_parser.add_argument("--z", type=float, required=True)
    xyz_parser.add_argument("--duration", type=float, default=1.0)
    xyz_parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    xyz_parser.set_defaults(_handler=_run_xyz)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_and_print(args._handler, args)


__all__ = ["build_parser", "main"]
