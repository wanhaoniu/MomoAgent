#!/usr/bin/env python3
"""Compatibility wrapper for consolidated CLI helpers."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


SDK_SRC = Path(__file__).resolve().parents[3] / 'sdk' / 'src'
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

module = importlib.import_module('soarmmoce_sdk.cli_common')
__all__ = list(getattr(module, '__all__', []))
__doc__ = getattr(module, '__doc__', __doc__)

for name, value in vars(module).items():
    if name.startswith('__') and name not in {'__all__', '__doc__'}:
        continue
    globals()[name] = value
