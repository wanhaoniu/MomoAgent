#!/usr/bin/env python3
"""Replay the last recorded pose sequence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
LEGACY_DIR = SCRIPT_DIR / "_legacy"
if str(LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(LEGACY_DIR))

from record_pose_roundtrip import DEFAULT_SAVE_PATH, main as legacy_main  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a recorded pose sequence JSON file")
    parser.add_argument("path", nargs="?", default="", help="Recorded JSON file or directory")
    parser.add_argument("--replay-path", default="", help="Recorded JSON file or directory")
    parser.add_argument("--move-duration-sec", type=float, default=5.0)
    parser.add_argument("--wait-between-poses", default="true")
    parser.add_argument("--skip-home", default="false")
    args, passthrough = parser.parse_known_args()

    replay_path = str(args.replay_path or args.path or DEFAULT_SAVE_PATH)
    sys.argv = [
        sys.argv[0],
        "--replay-path",
        replay_path,
        "--move-duration-sec",
        str(float(args.move_duration_sec)),
        "--wait-between-poses",
        str(args.wait_between_poses),
        "--skip-home",
        str(args.skip_home),
        *passthrough,
    ]
    legacy_main()


if __name__ == "__main__":
    main()
