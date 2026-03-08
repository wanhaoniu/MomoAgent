#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from typing import Optional

from soarmmoce_sdk import Robot


DEFAULT_SHARED_STATE = "/tmp/soarmmoce_mock_shared_state.json"


def _parse_frame(value: str) -> str:
    v = str(value or "").strip().lower()
    if v not in {"base", "tool"}:
        raise argparse.ArgumentTypeError("frame must be 'base' or 'tool'")
    return v


def _print_state(robot: Robot) -> None:
    state = robot.get_state()
    pose = state.tcp_pose
    print(
        "[state] connected=%s tcp_xyz=(%.6f, %.6f, %.6f) tcp_rpy=(%.6f, %.6f, %.6f)"
        % (
            state.connected,
            float(pose.xyz[0]),
            float(pose.xyz[1]),
            float(pose.xyz[2]),
            float(pose.rpy[0]),
            float(pose.rpy[1]),
            float(pose.rpy[2]),
        )
    )


def _print_help() -> None:
    print(
        "\nCommands:\n"
        "  state\n"
        "  move <x> <y> <z> [frame]\n"
        "  delta <dx> <dy> <dz> [frame]\n"
        "  gripper <ratio>\n"
        "  home\n"
        "  stop\n"
        "  quit\n"
    )


def _to_float(text: str) -> float:
    return float(str(text).strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manual SDK->GUI sync test (mock shared state)."
    )
    parser.add_argument("--config", default=None, help="Optional SDK config YAML path.")
    parser.add_argument("--duration", type=float, default=0.8, help="Default motion duration.")
    parser.add_argument("--timeout", type=float, default=3.0, help="Default wait timeout.")
    parser.add_argument(
        "--frame",
        type=_parse_frame,
        default="base",
        help="Default frame for move/delta commands.",
    )
    parser.add_argument(
        "--shared-state",
        default=DEFAULT_SHARED_STATE,
        help="Path for SOARMMOCE_MOCK_SHARED_STATE_FILE.",
    )
    args = parser.parse_args()

    # Keep this process on the same mock state source used by GUI.
    if args.shared_state:
        os.environ["SOARMMOCE_MOCK_SHARED_STATE_FILE"] = str(args.shared_state)

    robot = Robot.from_config(args.config) if args.config else Robot()
    print("[init] shared_state=%s" % os.getenv("SOARMMOCE_MOCK_SHARED_STATE_FILE", ""))
    print("[init] connecting ...")
    robot.connect()
    print("[init] transport=%s" % type(robot._transport).__name__)  # debug info only
    _print_state(robot)
    _print_help()

    default_frame = str(args.frame)
    duration = float(args.duration)
    timeout: Optional[float] = float(args.timeout)

    try:
        while True:
            raw = input("sdk-test> ").strip()
            if not raw:
                continue
            parts = raw.split()
            cmd = parts[0].lower()

            if cmd in {"q", "quit", "exit"}:
                break
            if cmd in {"h", "help", "?"}:
                _print_help()
                continue
            if cmd == "state":
                _print_state(robot)
                continue
            if cmd == "stop":
                robot.stop()
                print("[ok] stop")
                _print_state(robot)
                continue
            if cmd == "home":
                robot.home(duration=duration, wait=True, timeout=timeout)
                print("[ok] home")
                _print_state(robot)
                continue
            if cmd == "gripper":
                if len(parts) != 2:
                    print("[err] usage: gripper <ratio>")
                    continue
                ratio = max(0.0, min(1.0, _to_float(parts[1])))
                robot.set_gripper(open_ratio=ratio, wait=True, timeout=timeout)
                print("[ok] gripper ratio=%.3f" % ratio)
                _print_state(robot)
                continue
            if cmd == "move":
                if len(parts) not in {4, 5}:
                    print("[err] usage: move <x> <y> <z> [frame]")
                    continue
                x, y, z = _to_float(parts[1]), _to_float(parts[2]), _to_float(parts[3])
                frame = _parse_frame(parts[4]) if len(parts) == 5 else default_frame
                robot.move_tcp(
                    x=x,
                    y=y,
                    z=z,
                    rpy=None,
                    frame=frame,
                    duration=duration,
                    wait=True,
                    timeout=timeout,
                )
                print("[ok] move x=%.4f y=%.4f z=%.4f frame=%s" % (x, y, z, frame))
                _print_state(robot)
                continue
            if cmd == "delta":
                if len(parts) not in {4, 5}:
                    print("[err] usage: delta <dx> <dy> <dz> [frame]")
                    continue
                dx, dy, dz = _to_float(parts[1]), _to_float(parts[2]), _to_float(parts[3])
                frame = _parse_frame(parts[4]) if len(parts) == 5 else default_frame
                robot.move_tcp(
                    x=dx,
                    y=dy,
                    z=dz,
                    rpy=None,
                    frame=frame,
                    duration=duration,
                    wait=True,
                    timeout=timeout,
                )
                print("[ok] delta dx=%.4f dy=%.4f dz=%.4f frame=%s" % (dx, dy, dz, frame))
                _print_state(robot)
                continue

            print("[err] unknown command: %s" % cmd)
            _print_help()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            robot.disconnect()
        except Exception:
            pass
    print("[done] disconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
