#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility shim for GUI 3D view.

This module forwards kinematics utilities to the SDK implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve()
_candidates = [
    _here.parents[2] / "sdk" / "src",  # repo root/sdk/src
    _here.parents[2] / "soarmMoce_sdk" / "src",  # /SO102/soarmMoce_sdk/src
    _here.parents[3] / "sdk" / "src",  # monorepo root/sdk/src
    _here.parents[3] / "soarmMoce_sdk" / "src",  # repo root/soarmMoce_sdk/src
]
for _sdk_path in _candidates:
    if _sdk_path.exists() and str(_sdk_path) not in sys.path:
        sys.path.insert(0, str(_sdk_path))

try:
    from soarmMoce_sdk.kinematics import RobotModel
    from soarmMoce_sdk.kinematics.frames import (
        transform_from_xyz_rpy,
        transform_rot,
        transform_trans,
    )
except Exception as e:
    raise ImportError(f"Failed to import SDK kinematics: {e}")

__all__ = [
    "RobotModel",
    "transform_from_xyz_rpy",
    "transform_rot",
    "transform_trans",
]
