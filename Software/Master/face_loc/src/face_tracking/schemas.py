from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from face_tracking.target_center import resolve_target_center


@dataclass(slots=True)
class FramePacket:
    frame_id: int
    timestamp: float
    frame: np.ndarray


@dataclass(slots=True)
class FaceDetection:
    bbox: tuple[float, float, float, float]
    confidence: float
    landmarks: list[tuple[float, float]] | None = None
    NOSE_LANDMARK_INDEX = 2

    @property
    def x1(self) -> float:
        return float(self.bbox[0])

    @property
    def y1(self) -> float:
        return float(self.bbox[1])

    @property
    def x2(self) -> float:
        return float(self.bbox[2])

    @property
    def y2(self) -> float:
        return float(self.bbox[3])

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def bbox_center(self) -> tuple[float, float]:
        return (self.x1 + self.width / 2.0, self.y1 + self.height / 2.0)

    @property
    def center(self) -> tuple[float, float]:
        if self.landmarks and len(self.landmarks) > self.NOSE_LANDMARK_INDEX:
            nose = self.landmarks[self.NOSE_LANDMARK_INDEX]
            return (float(nose[0]), float(nose[1]))
        return self.bbox_center

    @property
    def area(self) -> float:
        return self.width * self.height

    def to_payload(self, frame_size: tuple[int, int]) -> dict[str, Any]:
        frame_width, frame_height = frame_size
        frame_area = max(float(frame_width * frame_height), 1.0)
        return {
            "bbox": [round(self.x1, 3), round(self.y1, 3), round(self.x2, 3), round(self.y2, 3)],
            "center": [round(self.center[0], 3), round(self.center[1], 3)],
            "bbox_center": [round(self.bbox_center[0], 3), round(self.bbox_center[1], 3)],
            "size": [round(self.width, 3), round(self.height, 3)],
            "area_ratio": round(self.area / frame_area, 6),
            "confidence": round(float(self.confidence), 6),
            "landmarks": None
            if not self.landmarks
            else [[round(point[0], 3), round(point[1], 3)] for point in self.landmarks],
        }


@dataclass(slots=True)
class SmoothedState:
    center: tuple[float, float]
    area_ratio: float


def compute_offset_payload(
    center: tuple[float, float],
    frame_size: tuple[int, int],
    *,
    target_center: tuple[float, float] | None = None,
) -> dict[str, float]:
    frame_width, frame_height = frame_size
    half_width = max(frame_width / 2.0, 1.0)
    half_height = max(frame_height / 2.0, 1.0)
    target_center_x, target_center_y = target_center if target_center is not None else resolve_target_center(frame_size)

    dx = float(center[0] - target_center_x)
    dy = float(center[1] - target_center_y)
    return {
        "dx": round(dx, 3),
        "dy": round(dy, 3),
        "ndx": round(dx / half_width, 6),
        "ndy": round(dy / half_height, 6),
    }


def zero_offset_payload() -> dict[str, float]:
    return {"dx": 0.0, "dy": 0.0, "ndx": 0.0, "ndy": 0.0}
