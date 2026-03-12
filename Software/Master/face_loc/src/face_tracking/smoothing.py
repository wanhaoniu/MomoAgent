from __future__ import annotations

from face_tracking.config import SmoothingConfig
from face_tracking.schemas import SmoothedState


class FaceTrackerSmoother:
    def __init__(self, config: SmoothingConfig) -> None:
        self.config = config
        self._center: tuple[float, float] | None = None
        self._area_ratio: float | None = None
        self._missing_frames = 0

    def update(self, center: tuple[float, float], area_ratio: float) -> SmoothedState:
        if not self.config.enabled or self._center is None or self._area_ratio is None:
            self._center = center
            self._area_ratio = area_ratio
        else:
            self._center = (
                self.config.alpha_center * center[0] + (1.0 - self.config.alpha_center) * self._center[0],
                self.config.alpha_center * center[1] + (1.0 - self.config.alpha_center) * self._center[1],
            )
            self._area_ratio = self.config.alpha_area * area_ratio + (1.0 - self.config.alpha_area) * self._area_ratio

        self._missing_frames = 0
        return SmoothedState(center=self._center, area_ratio=self._area_ratio)

    def on_miss(self) -> None:
        self._missing_frames += 1
        if self._missing_frames >= self.config.max_missing_frames_before_reset:
            self.reset()

    def current(self) -> SmoothedState | None:
        if self._center is None or self._area_ratio is None:
            return None
        return SmoothedState(center=self._center, area_ratio=self._area_ratio)

    def reset(self) -> None:
        self._center = None
        self._area_ratio = None
        self._missing_frames = 0
