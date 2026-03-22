#!/usr/bin/env python3
"""Gripper entrypoints for soarmMoce without installed gripper."""

from __future__ import annotations

import argparse

from soarmmoce_cli_common import cli_bool, print_error, print_success
from soarmmoce_sdk import SoArmMoceController


def main() -> None:
    parser = argparse.ArgumentParser(description="soarmMoce gripper CLI (unsupported on current hardware)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Set gripper open ratio")
    p_set.add_argument("--open-ratio", type=float, required=True)
    p_set.add_argument("--duration", type=float, default=1.0)
    p_set.add_argument("--wait", type=cli_bool, default=True)
    p_set.add_argument("--timeout", type=float, default=None)

    p_open = sub.add_parser("open", help="Open gripper fully")
    p_open.add_argument("--duration", type=float, default=1.0)
    p_open.add_argument("--wait", type=cli_bool, default=True)
    p_open.add_argument("--timeout", type=float, default=None)

    p_close = sub.add_parser("close", help="Close gripper fully")
    p_close.add_argument("--duration", type=float, default=1.0)
    p_close.add_argument("--wait", type=cli_bool, default=True)
    p_close.add_argument("--timeout", type=float, default=None)

    args = parser.parse_args()

    try:
        arm = SoArmMoceController()
        if args.cmd == "set":
            result = arm.set_gripper(
                open_ratio=args.open_ratio,
                duration=args.duration,
                wait=args.wait,
                timeout=args.timeout,
            )
        elif args.cmd == "open":
            result = arm.open_gripper(duration=args.duration, wait=args.wait, timeout=args.timeout)
        else:
            result = arm.close_gripper(duration=args.duration, wait=args.wait, timeout=args.timeout)
        print_success(result)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
