from __future__ import annotations

import math

from face_tracking.config import SelectionConfig
from face_tracking.schemas import FaceDetection


class TargetSelector:
    def __init__(self, config: SelectionConfig) -> None:
        self.config = config

    @property
    def strategy_name(self) -> str:
        return self.config.strategy

    def select(self, detections: list[FaceDetection], frame_shape: tuple[int, ...]) -> FaceDetection | None:
        if not detections:
            return None

        if self.config.strategy == "highest_confidence":
            return max(detections, key=lambda item: item.confidence)

        if self.config.strategy == "closest_to_center":
            frame_height, frame_width = frame_shape[:2]
            frame_center = (frame_width / 2.0, frame_height / 2.0)
            return min(
                detections,
                key=lambda item: math.hypot(item.center[0] - frame_center[0], item.center[1] - frame_center[1]),
            )

        return max(detections, key=lambda item: item.area)
