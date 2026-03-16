from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "mock_target_point_service.py"
SPEC = importlib.util.spec_from_file_location("mock_target_point_service_runtime", str(MODULE_PATH))
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_fixed_mode_returns_requested_center() -> None:
    generator = MODULE.MockTargetGenerator(
        width=1280,
        height=720,
        mode="fixed",
        center_x=960.0,
        center_y=180.0,
        amplitude_x=0.0,
        amplitude_y=0.0,
        period_sec=4.0,
        radius_x=0.0,
        radius_y=0.0,
        smoothing_alpha=1.0,
    )
    payload = generator.latest()
    assert payload["target"]["center"] == [960.0, 180.0]
    assert payload["smoothed_target"]["center"] == [960.0, 180.0]
    assert payload["status"] == "tracking"


def test_circle_mode_stays_inside_frame() -> None:
    generator = MODULE.MockTargetGenerator(
        width=1280,
        height=720,
        mode="circle",
        center_x=640.0,
        center_y=360.0,
        amplitude_x=0.0,
        amplitude_y=0.0,
        period_sec=4.0,
        radius_x=220.0,
        radius_y=120.0,
        smoothing_alpha=0.35,
    )
    payload_1 = generator.latest()
    payload_2 = generator.latest()
    assert payload_2["frame_id"] == payload_1["frame_id"] + 1
    cx, cy = payload_2["target"]["center"]
    assert 0.0 <= cx <= 1280.0
    assert 0.0 <= cy <= 720.0


def test_script_loader_accepts_json_array(tmp_path: Path) -> None:
    script_path = tmp_path / "mock_targets.json"
    script_path.write_text(
        json.dumps(
            [
                {"center": [100, 120], "duration_sec": 1.0, "label": "a"},
                {"center": {"x": 300, "y": 320}, "duration_sec": 1.0, "label": "b"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entries = MODULE._load_script_entries(str(script_path))
    assert len(entries) == 2
    assert entries[0]["center"] == [100, 120]


def test_scripted_mode_uses_script_entries() -> None:
    generator = MODULE.MockTargetGenerator(
        width=1280,
        height=720,
        mode="scripted",
        center_x=640.0,
        center_y=360.0,
        amplitude_x=0.0,
        amplitude_y=0.0,
        period_sec=4.0,
        radius_x=0.0,
        radius_y=0.0,
        smoothing_alpha=1.0,
        script_entries=[
            {"center": [111, 222], "duration_sec": 1.0, "label": "first"},
            {"center": [333, 444], "duration_sec": 1.0, "label": "second"},
        ],
        loop_script=False,
    )
    payload = generator.latest()
    assert payload["target"]["center"] == [111.0, 222.0]
    assert payload["target"]["label"] == "first"


def test_arm_feedback_mode_reads_mock_shared_state(tmp_path: Path) -> None:
    shared_state = tmp_path / "mock_shared_state.json"
    shared_state.write_text(
        json.dumps(
            {
                "q_start": [0.0, 0.0, 0.0, 0.0, 0.0],
                "q_target": [0.2, 0.1, -0.05, 0.0, 0.0],
                "motion_start_time": 0.0,
                "motion_end_time": 0.0,
            }
        ),
        encoding="utf-8",
    )
    generator = MODULE.MockTargetGenerator(
        width=1280,
        height=720,
        mode="arm-feedback",
        center_x=640.0,
        center_y=360.0,
        amplitude_x=0.0,
        amplitude_y=0.0,
        period_sec=4.0,
        radius_x=0.0,
        radius_y=0.0,
        smoothing_alpha=1.0,
        shared_state_path=str(shared_state),
        base_ndx=0.25,
        base_ndy=0.12,
        pan_ndx_per_rad=1.8,
        tilt_ndy_per_rad=1.0,
        tilt_secondary_ndy_per_rad=0.8,
    )
    payload_1 = generator.latest()
    assert payload_1["target"]["center"] == [800.0, 403.2]

    shared_state.write_text(
        json.dumps(
            {
                "q_start": [0.0, 0.0, 0.0, 0.0, 0.0],
                "q_target": [0.0, 0.0, 0.0, 0.0, 0.0],
                "motion_start_time": 0.0,
                "motion_end_time": 0.0,
            }
        ),
        encoding="utf-8",
    )
    payload_2 = generator.latest()
    assert payload_2["target"]["center"] == [1030.4, 381.6]
