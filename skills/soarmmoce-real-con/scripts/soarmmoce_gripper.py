#!/usr/bin/env python3
"""Compatibility wrapper for consolidated gripper CLI."""

from __future__ import annotations

import sys
from pathlib import Path


SDK_SRC = Path(__file__).resolve().parents[3] / 'sdk' / 'src'
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk.cli.gripper import main


if __name__ == '__main__':
    main()
