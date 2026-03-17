from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from gesture_tracking.config import SourceConfig


@dataclass(slots=True)
class FramePacket:
    frame_id: int
    timestamp: float
    frame: np.ndarray


class OpenCvFrameSource:
    def __init__(self, config: SourceConfig) -> None:
        self._config = config
        self._capture = None
        self._frame_id = 0

    def start(self) -> None:
        if self._capture is not None:
            return
        source: Any
        if self._config.type == "camera":
            source = int(self._config.camera_index)
        else:
            source = str(self._config.capture_uri or "").strip()
            if not source:
                raise RuntimeError("capture_uri is required when source.type=capture")
        api_preference = cv2.CAP_ANY
        if str(self._config.api_preference).strip().lower() == "v4l2":
            api_preference = cv2.CAP_V4L2
        self._capture = cv2.VideoCapture(source, api_preference)
        if self._config.width:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._config.width))
        if self._config.height:
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._config.height))
        if self._config.fps:
            self._capture.set(cv2.CAP_PROP_FPS, float(self._config.fps))
        if not self._capture.isOpened():
            self.close()
            raise RuntimeError(f"Failed to open video source: {source}")

    def read(self, timestamp: float) -> FramePacket:
        if self._capture is None:
            raise RuntimeError("video source has not been started")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise RuntimeError("Failed to read frame from video source")
        rotation = int(self._config.rotation_deg) % 360
        if rotation == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif rotation == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        self._frame_id += 1
        return FramePacket(frame_id=self._frame_id, timestamp=timestamp, frame=frame)

    def describe(self) -> dict[str, Any]:
        return {
            "type": self._config.type,
            "camera_index": int(self._config.camera_index),
            "capture_uri": self._config.capture_uri,
            "width": self._config.width,
            "height": self._config.height,
            "fps": self._config.fps,
            "rotation_deg": self._config.rotation_deg,
        }

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
