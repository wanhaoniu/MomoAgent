#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Legacy kinematics fallback entry.

The old GUI kinematics backend depended on a removed SDK module. Keep this file as a
clear failure point so callers do not silently import stale paths.
"""

from __future__ import annotations

raise ImportError(
    "The legacy ik_solver backend was removed because it depended on the deleted "
    "'soarmMoce_sdk.kinematics' package. Use the PyBullet/VTK simulation backends, "
    "or add a new IK backend explicitly."
)
