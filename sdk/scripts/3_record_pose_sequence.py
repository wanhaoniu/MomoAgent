#!/usr/bin/env python3
"""Record and replay a pose sequence from the public scripts directory."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
LEGACY_DIR = SCRIPT_DIR / "_legacy"
if str(LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(LEGACY_DIR))

from record_pose_roundtrip import main  # noqa: E402


if __name__ == "__main__":
    main()
