from face_tracking.config import HintConfig
from face_tracking.controller import MirrorFollowControllerHint


def test_controller_generates_expected_direction_hints() -> None:
    controller = MirrorFollowControllerHint(
        HintConfig(dead_zone_ndx=0.05, dead_zone_ndy=0.05, min_face_area_ratio=0.10, max_face_area_ratio=0.30)
    )
    payload = controller.compute(
        raw_offset={"dx": 40.0, "dy": -20.0, "ndx": 0.20, "ndy": -0.10},
        smoothed_offset={"dx": 30.0, "dy": -10.0, "ndx": 0.15, "ndy": -0.08},
        raw_area_ratio=0.05,
        smoothed_area_ratio=0.06,
        detected=True,
    )
    assert payload["lateral_hint"] == "RIGHT"
    assert payload["vertical_hint"] == "UP"
    assert payload["distance_hint"] == "FORWARD"
    assert payload["combined_hint"] == ["RIGHT", "UP", "FORWARD"]


def test_controller_holds_when_no_detection() -> None:
    controller = MirrorFollowControllerHint(HintConfig())
    payload = controller.compute(
        raw_offset={"dx": 0.0, "dy": 0.0, "ndx": 0.0, "ndy": 0.0},
        smoothed_offset={"dx": 0.0, "dy": 0.0, "ndx": 0.0, "ndy": 0.0},
        raw_area_ratio=0.0,
        smoothed_area_ratio=0.0,
        detected=False,
    )
    assert payload["combined_hint"] == ["HOLD"]
