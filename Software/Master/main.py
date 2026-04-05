#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clean launcher for the current Master HMI.

The old ``main.py`` mixed together GUI startup, leader-following transport,
file storage, and a deprecated CLI controller. That made the entry point hard
to reason about and easy to break.

The rebuilt version keeps one responsibility only: prepare a few session-level
runtime flags, then launch the refactored Qt GUI from ``gui.py``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the MomoAgent Master GUI")
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Optional SDK YAML config path. Exported to SOARMMOCE_CONFIG for this session.",
    )
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=None,
        help="Optional GUI SDK sync interval in seconds. Exported to SOARMMOCE_GUI_SYNC_INTERVAL.",
    )
    return parser


def _apply_session_overrides(args: argparse.Namespace) -> None:
    config_path = str(args.config or "").strip()
    if config_path:
        os.environ["SOARMMOCE_CONFIG"] = str(Path(config_path).expanduser().resolve())

    if args.sync_interval is not None:
        os.environ["SOARMMOCE_GUI_SYNC_INTERVAL"] = str(max(0.02, float(args.sync_interval)))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, qt_args = parser.parse_known_args(argv)
    _apply_session_overrides(args)

    # QApplication still inspects sys.argv, so keep only the unconsumed tail.
    sys.argv = [sys.argv[0], *qt_args]

    from gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
