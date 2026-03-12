from face_tracking.config import SmoothingConfig
from face_tracking.smoothing import FaceTrackerSmoother


def test_smoother_blends_center_and_area() -> None:
    smoother = FaceTrackerSmoother(SmoothingConfig(enabled=True, alpha_center=0.5, alpha_area=0.5))
    first = smoother.update((100.0, 100.0), 0.10)
    second = smoother.update((200.0, 200.0), 0.30)
    assert first.center == (100.0, 100.0)
    assert second.center == (150.0, 150.0)
    assert second.area_ratio == 0.20


def test_smoother_resets_after_missing_frames() -> None:
    smoother = FaceTrackerSmoother(SmoothingConfig(max_missing_frames_before_reset=2))
    smoother.update((100.0, 100.0), 0.10)
    smoother.on_miss()
    assert smoother.current() is not None
    smoother.on_miss()
    assert smoother.current() is None
