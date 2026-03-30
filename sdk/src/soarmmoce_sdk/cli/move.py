from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from ..cli_common import cli_bool, print_error, print_success
from ..real_arm import SoArmMoceController


def main() -> None:
    parser = argparse.ArgumentParser(description="soarmMoce arm motion CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_delta = sub.add_parser("delta", help="Move TCP by relative dx/dy/dz")
    p_delta.add_argument("--dx", type=float, default=0.0)
    p_delta.add_argument("--dy", type=float, default=0.0)
    p_delta.add_argument("--dz", type=float, default=0.0)
    p_delta.add_argument(
        "--frame",
        choices=["base", "urdf", "user", "tool"],
        default="base",
        help="base=raw URDF/sim frame, urdf=base alias, user=x forward y left z up, tool=current tool frame",
    )
    p_delta.add_argument("--duration", type=float, default=1.0)
    p_delta.add_argument("--wait", type=cli_bool, default=True)
    p_delta.add_argument("--timeout", type=float, default=None)
    p_delta.add_argument("--trace", action="store_true")

    p_xyz = sub.add_parser("xyz", help="Move TCP to absolute x/y/z")
    p_xyz.add_argument("--x", type=float, default=None)
    p_xyz.add_argument("--y", type=float, default=None)
    p_xyz.add_argument("--z", type=float, default=None)
    p_xyz.add_argument("--duration", type=float, default=1.0)
    p_xyz.add_argument("--wait", type=cli_bool, default=True)
    p_xyz.add_argument("--timeout", type=float, default=None)
    p_xyz.add_argument("--trace", action="store_true")

    p_joint = sub.add_parser("joint", help="Move one joint")
    p_joint.add_argument("--joint", required=True)
    p_joint.add_argument("--delta-deg", type=float, default=None)
    p_joint.add_argument("--target-deg", type=float, default=None)
    p_joint.add_argument("--duration", type=float, default=1.0)
    p_joint.add_argument("--wait", type=cli_bool, default=True)
    p_joint.add_argument("--timeout", type=float, default=None)
    p_joint.add_argument("--trace", action="store_true")

    p_joints = sub.add_parser("joints", help="Move multiple joints with JSON targets")
    p_joints.add_argument("--targets-json", required=True, help='JSON object, e.g. {"wrist_roll": 5}')
    p_joints.add_argument("--duration", type=float, default=1.0)
    p_joints.add_argument("--wait", type=cli_bool, default=True)
    p_joints.add_argument("--timeout", type=float, default=None)
    p_joints.add_argument("--trace", action="store_true")

    p_home = sub.add_parser("home", help="Move to configured home pose")
    p_home.add_argument("--duration", type=float, default=1.5)
    p_home.add_argument("--wait", type=cli_bool, default=True)
    p_home.add_argument("--timeout", type=float, default=None)
    p_home.add_argument("--trace", action="store_true")

    p_init_home = sub.add_parser("init_home", help="Initialize the multi-turn runtime session from the calibrated home pose")
    p_init_home.add_argument(
        "--recover",
        action="store_true",
        help="If direct init_home fails because the arm is only approximately at home, first move multi-turn joints toward calibrated home",
    )
    p_init_home.add_argument(
        "--duration",
        type=float,
        default=1.0,
        help="Recovery move duration in seconds when --recover is used",
    )
    p_init_home.add_argument("--timeout", type=float, default=None)
    p_init_home.add_argument(
        "--recover-max-delta-raw",
        type=float,
        default=768.0,
        help="Abort recovery if any multi-turn joint is farther than this wrapped raw delta from calibrated home",
    )

    p_zero = sub.add_parser("zero", help="Deprecated alias for init_home")
    p_zero.add_argument("--recover", action="store_true")
    p_zero.add_argument("--duration", type=float, default=1.0)
    p_zero.add_argument("--timeout", type=float, default=None)
    p_zero.add_argument("--recover-max-delta-raw", type=float, default=768.0)
    sub.add_parser("stop", help="Hold current pose")

    args = parser.parse_args()

    try:
        arm = SoArmMoceController()
        if args.cmd == "delta":
            result = arm.move_delta(
                dx=args.dx,
                dy=args.dy,
                dz=args.dz,
                frame=args.frame,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
                trace=args.trace,
            )
        elif args.cmd == "xyz":
            result = arm.move_to(
                x=args.x,
                y=args.y,
                z=args.z,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
                trace=args.trace,
            )
        elif args.cmd == "joint":
            result = arm.move_joint(
                joint=args.joint,
                delta_deg=args.delta_deg,
                target_deg=args.target_deg,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
                trace=args.trace,
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
                trace=args.trace,
            )
        elif args.cmd == "home":
            result = arm.home(duration=args.duration, wait=args.wait, timeout=args.timeout, trace=args.trace)
        elif args.cmd in {"init_home", "zero"}:
            if bool(getattr(args, "recover", False)):
                recovery = arm.recover_multi_turn_home(
                    duration=float(getattr(args, "duration", 1.0)),
                    wait=True,
                    timeout=getattr(args, "timeout", None),
                    max_delta_raw=float(getattr(args, "recover_max_delta_raw", 768.0)),
                )
                result = arm.init_multi_turn_home()
                result["recovery"] = recovery
            else:
                result = arm.init_multi_turn_home()
        else:
            result = arm.stop()
        print_success(result)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
