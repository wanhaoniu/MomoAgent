#!/usr/bin/env python3
"""Compatibility wrapper for the consolidated SDK implementation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
IMPL_PATH = REPO_ROOT / "sdk" / "src" / "soarmmoce_sdk" / "real_arm.py"
MODULE_NAME = "_soarmmoce_sdk_real_arm"

if not IMPL_PATH.exists():
    raise ImportError(f"Consolidated SDK implementation not found: {IMPL_PATH}")

spec = importlib.util.spec_from_file_location(MODULE_NAME, IMPL_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Could not load consolidated SDK implementation: {IMPL_PATH}")

module = importlib.util.module_from_spec(spec)
sys.modules.setdefault(MODULE_NAME, module)
spec.loader.exec_module(module)

__all__ = list(getattr(module, "__all__", []))
__doc__ = getattr(module, "__doc__", __doc__)

for name, value in vars(module).items():
    if name.startswith("__") and name not in {"__all__", "__doc__"}:
        continue
    globals()[name] = value
