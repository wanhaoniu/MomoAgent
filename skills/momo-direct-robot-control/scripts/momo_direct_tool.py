#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if SDK_SRC.exists() and str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable


def _json_loads(raw: str, *, expected_type: type) -> Any:
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON: {exc}") from exc
    if not isinstance(value, expected_type):
        raise SystemExit(f"expected JSON {expected_type.__name__}")
    return value


def _state_summary(state: Any) -> dict[str, Any]:
    payload = to_jsonable(state)
    joint_state = dict(payload.get("joint_state") or {})
    joints_deg = {
        joint_name: joint_state.get(joint_name)
        for joint_name in JOINTS
        if joint_name in joint_state
    }
    return {
        "timestamp": payload.get("timestamp"),
        "joint_order": list(joint_state.get("names") or JOINTS),
        "joints_deg": joints_deg,
        "joints_rad": list(joint_state.get("q") or []),
        "tcp_pose": payload.get("tcp_pose"),
        "gripper": payload.get("gripper_state"),
        "raw_present_position": payload.get("raw_present_position"),
        "relative_raw_position": payload.get("relative_raw_position"),
    }


def _result_summary(result: Any) -> dict[str, Any]:
    payload = to_jsonable(result)
    if not isinstance(payload, dict):
        return {"value": payload}
    summary: dict[str, Any] = {}
    for key in (
        "action",
        "target_deg",
        "targets_deg",
        "goal_raw",
        "duration_sec",
        "duration_source",
        "speed_percent",
        "frame",
        "delta",
        "target_xyz_m",
        "target_rpy_rad",
        "ik",
        "wait",
        "settled",
    ):
        if key in payload:
            summary[key] = payload[key]
    if "state" in payload:
        summary["state"] = _state_summary(payload["state"])
    return summary


def _print(payload: dict[str, Any], *, ok: bool = True) -> int:
    out = {"ok": bool(ok), **payload}
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


def _build_controller(config_path: str) -> SoArmMoceController:
    resolved_path = str(config_path or "").strip() or None
    return SoArmMoceController(resolve_config(resolved_path))


def _run_with_arm(args: argparse.Namespace, callback) -> int:
    arm = _build_controller(args.config)
    try:
        return callback(arm)
    except Exception as exc:  # noqa: BLE001
        return _print(
            {
                "error": str(exc).strip() or exc.__class__.__name__,
                "error_type": exc.__class__.__name__,
                "command": args.command,
            },
            ok=False,
        )
    finally:
        try:
            arm.close(disable_torque=bool(args.release_torque_on_exit))
        except Exception:
            pass


def _add_motion_args(parser: argparse.ArgumentParser, *, default_duration: float, default_speed: int, default_timeout: float) -> None:
    parser.add_argument("--duration", type=float, default=default_duration)
    parser.add_argument("--speed-percent", type=int, default=default_speed)
    parser.add_argument("--timeout", type=float, default=default_timeout)
    parser.add_argument("--no-wait", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Direct SDK control helper for the MomoAgent arm.")
    parser.add_argument(
        "--config",
        default=os.getenv("SOARMMOCE_CONFIG", ""),
        help="Optional SDK YAML config path. Defaults to SOARMMOCE_CONFIG or SDK default.",
    )
    parser.add_argument(
        "--release-torque-on-exit",
        action="store_true",
        help="Disable torque when closing. Use only when the arm is physically supported.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("state", help="Read current robot state")
    subparsers.add_parser("stop", help="Hold current raw positions")

    joint_parser = subparsers.add_parser("joint", help="Move one joint")
    joint_parser.add_argument("--joint", choices=list(JOINTS), required=True)
    group = joint_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--delta-deg", type=float)
    group.add_argument("--target-deg", type=float)
    _add_motion_args(joint_parser, default_duration=1.0, default_speed=30, default_timeout=4.0)

    joints_parser = subparsers.add_parser("joints", help="Move multiple joints to target degrees")
    joints_parser.add_argument("--targets", required=True, help='JSON object, for example {"shoulder_pan": 0}')
    _add_motion_args(joints_parser, default_duration=1.5, default_speed=30, default_timeout=5.0)

    delta_parser = subparsers.add_parser("move-delta", help="Move TCP by a Cartesian delta")
    delta_parser.add_argument("--dx", type=float, default=0.0)
    delta_parser.add_argument("--dy", type=float, default=0.0)
    delta_parser.add_argument("--dz", type=float, default=0.0)
    delta_parser.add_argument("--drx", type=float, default=0.0)
    delta_parser.add_argument("--dry", type=float, default=0.0)
    delta_parser.add_argument("--drz", type=float, default=0.0)
    delta_parser.add_argument("--frame", choices=["base", "tool"], default="base")
    _add_motion_args(delta_parser, default_duration=1.0, default_speed=25, default_timeout=5.0)

    pose_parser = subparsers.add_parser("pose", help="Move TCP to an absolute pose")
    pose_parser.add_argument("--xyz", required=True, help="JSON list of 3 meters, for example [0.25,0,0.2]")
    pose_parser.add_argument("--rpy", default="", help="Optional JSON list of 3 radians")
    pose_parser.add_argument("--seed-policy", default="current", choices=["current", "home", "startup", "zero"])
    _add_motion_args(pose_parser, default_duration=2.0, default_speed=25, default_timeout=8.0)

    gripper_parser = subparsers.add_parser("gripper", help="Set gripper open ratio")
    gripper_parser.add_argument("--open-ratio", type=float, required=True)
    _add_motion_args(gripper_parser, default_duration=1.0, default_speed=40, default_timeout=3.0)

    home_parser = subparsers.add_parser("home", help="Move to SDK home/startup reference")
    _add_motion_args(home_parser, default_duration=1.5, default_speed=30, default_timeout=5.0)

    torque_parser = subparsers.add_parser("torque", help="Enable or disable torque")
    torque_group = torque_parser.add_mutually_exclusive_group(required=True)
    torque_group.add_argument("--enable", action="store_true")
    torque_group.add_argument("--disable", action="store_true")

    args = parser.parse_args(argv)

    def run(arm: SoArmMoceController) -> int:
        before = None
        if args.command != "state":
            before = _state_summary(arm.get_state())

        if args.command == "state":
            return _print({"command": "state", "state": _state_summary(arm.get_state())})

        if args.command == "stop":
            result = arm.stop()
            return _print(
                {
                    "command": "stop",
                    "before": before,
                    "result": _result_summary(result),
                    "after": _state_summary(arm.get_state()),
                }
            )

        if args.command == "joint":
            kwargs: dict[str, Any] = {
                "joint": args.joint,
                "duration": args.duration,
                "speed_percent": args.speed_percent,
                "wait": not bool(args.no_wait),
                "timeout": args.timeout,
            }
            if args.delta_deg is not None:
                kwargs["delta_deg"] = args.delta_deg
            else:
                kwargs["target_deg"] = args.target_deg
            result = arm.move_joint(**kwargs)
            return _print({"command": "joint", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "joints":
            targets = _json_loads(args.targets, expected_type=dict)
            result = arm.move_joints(
                {str(name): float(value) for name, value in targets.items()},
                duration=args.duration,
                speed_percent=args.speed_percent,
                wait=not bool(args.no_wait),
                timeout=args.timeout,
            )
            return _print({"command": "joints", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "move-delta":
            result = arm.move_delta(
                dx=args.dx,
                dy=args.dy,
                dz=args.dz,
                drx=args.drx,
                dry=args.dry,
                drz=args.drz,
                frame=args.frame,
                duration=args.duration,
                speed_percent=args.speed_percent,
                wait=not bool(args.no_wait),
                timeout=args.timeout,
            )
            return _print({"command": "move-delta", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "pose":
            xyz = _json_loads(args.xyz, expected_type=list)
            rpy = _json_loads(args.rpy, expected_type=list) if str(args.rpy or "").strip() else None
            result = arm.move_pose(
                xyz=xyz,
                rpy=rpy,
                seed_policy=args.seed_policy,
                duration=args.duration,
                speed_percent=args.speed_percent,
                wait=not bool(args.no_wait),
                timeout=args.timeout,
            )
            return _print({"command": "pose", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "gripper":
            result = arm.set_gripper(
                open_ratio=max(0.0, min(1.0, float(args.open_ratio))),
                duration=args.duration,
                speed_percent=args.speed_percent,
                wait=not bool(args.no_wait),
                timeout=args.timeout,
            )
            return _print({"command": "gripper", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "home":
            result = arm.home(
                duration=args.duration,
                speed_percent=args.speed_percent,
                wait=not bool(args.no_wait),
                timeout=args.timeout,
            )
            return _print({"command": "home", "before": before, "result": _result_summary(result), "after": _state_summary(arm.get_state())})

        if args.command == "torque":
            if args.enable:
                arm.enable_torque()
                action = "enable_torque"
            else:
                arm.disable_torque()
                action = "disable_torque"
            return _print({"command": "torque", "action": action, "before": before, "after": _state_summary(arm.get_state())})

        return _print({"error": f"unsupported command: {args.command}"}, ok=False)

    return _run_with_arm(args, run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
