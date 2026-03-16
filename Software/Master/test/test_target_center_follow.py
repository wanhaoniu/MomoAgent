from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "target_center_follow.py"
SPEC = importlib.util.spec_from_file_location("target_center_follow_runtime", str(MODULE_PATH))
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_extract_observation_prefers_smoothed_offset() -> None:
    payload = {
        "timestamp": 10.0,
        "frame_id": 4,
        "offset": {"ndx": 0.2, "ndy": -0.1},
        "smoothed_offset": {"ndx": 0.15, "ndy": -0.08},
        "frame_size": [1280, 720],
    }
    obs = MODULE.extract_target_observation(payload)
    assert obs.raw_ndx == 0.2
    assert obs.raw_ndy == -0.1
    assert obs.ndx == 0.15
    assert obs.ndy == -0.08


def test_extract_observation_from_pixel_center() -> None:
    payload = {
        "timestamp": 11.0,
        "frame_id": 5,
        "target": {"center": [960, 180]},
        "frame_size": [1280, 720],
    }
    obs = MODULE.extract_target_observation(payload)
    assert round(obs.raw_ndx, 6) == 0.5
    assert round(obs.raw_ndy, 6) == -0.5
    assert obs.center == (960.0, 180.0)


def test_extract_observation_from_normalized_center() -> None:
    payload = {
        "timestamp": 12.0,
        "frame_id": 6,
        "interesting_target": {"center_norm": [0.75, 0.25]},
    }
    obs = MODULE.extract_target_observation(payload)
    assert round(obs.raw_ndx, 6) == 0.5
    assert round(obs.raw_ndy, 6) == -0.5


def test_extract_observation_rejects_missing_target() -> None:
    payload = {
        "timestamp": 13.0,
        "frame_id": 7,
        "detected": False,
        "status": "no_target",
    }
    try:
        MODULE.extract_target_observation(payload)
    except MODULE.TargetPayloadError as exc:
        assert "detected=false" in str(exc)
    else:
        raise AssertionError("expected TargetPayloadError")


def test_normalize_probe_policy() -> None:
    assert MODULE._normalize_probe_policy("strict") == "strict"
    assert MODULE._normalize_probe_policy("disable-axis") == "disable-axis"
    assert MODULE._normalize_probe_policy("") == "skip-optional"
