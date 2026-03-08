#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for soarmmoce-openclaw-skill dispatcher (mock mode).

Run:
  python3 Software/Master/test_skiil_sdk.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _ensure_local_import_path() -> None:
    """Allow running directly from repo without pip install."""
    repo_root = Path(__file__).resolve().parents[2]
    skill_src = repo_root / "packages" / "soarmmoce-openclaw-skill" / "src"
    sdk_src = repo_root / "sdk" / "src"

    if skill_src.exists():
        sys.path.insert(0, str(skill_src))
    if sdk_src.exists():
        sys.path.insert(0, str(sdk_src))


_ensure_local_import_path()

from soarmmoce_openclaw_skill import dispatch  # noqa: E402
from soarmmoce_openclaw_skill.dispatcher import reset_robot  # noqa: E402


def _run_tool(name: str, args: dict) -> dict:
    resp = dispatch(name, args)
    print(f"\n=== {name} ===")
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return resp


def main() -> int:
    # Force mock mode for safe local testing.
    os.environ["SOARMMOCE_TRANSPORT"] = "mock"
    os.environ.pop("SOARMMOCE_CONFIG", None)

    reset_robot()
    try:
        r0 = _run_tool("get_robot_state", {})
        if not r0.get("ok"):
            return 1

        # Use current pose as target to avoid unreachable IK in smoke tests.
        xyz = r0["result"]["state"]["tcp_pose"]["xyz"]
        r1 = _run_tool(
            "move_robot_arm",
            {
                "x": float(xyz[0]),
                "y": float(xyz[1]),
                "z": float(xyz[2]),
                "frame": "base",
                "duration": 0.01,
                "wait": True,
                "timeout": 1.0,
            },
        )
        if not r1.get("ok"):
            return 2

        r2 = _run_tool("set_gripper", {"open_ratio": 0.5, "wait": True, "timeout": 1.0})
        if not r2.get("ok"):
            return 3

        r3 = _run_tool("home", {"duration": 0.01, "wait": True, "timeout": 1.0})
        if not r3.get("ok"):
            return 4

        r4 = _run_tool("stop_robot", {})
        if not r4.get("ok"):
            return 5

        print("\nSkill SDK smoke test passed.")
        return 0
    finally:
        reset_robot()


if __name__ == "__main__":
    raise SystemExit(main())
