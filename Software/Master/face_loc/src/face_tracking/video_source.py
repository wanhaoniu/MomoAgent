from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from face_tracking.config import SourceConfig


CAPTURE_API_MAP = {
    "auto": None,
    "v4l2": cv2.CAP_V4L2,
    "gstreamer": cv2.CAP_GSTREAMER,
    "ffmpeg": cv2.CAP_FFMPEG,
    "images": cv2.CAP_IMAGES,
}


def _normalize_rotation_deg(value: int) -> int:
    rotation = int(value) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation_deg must be one of 0, 90, 180, 270")
    return rotation


def _rotate_frame(frame: np.ndarray, rotation_deg: int) -> np.ndarray:
    rotation = _normalize_rotation_deg(rotation_deg)
    if rotation == 0:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


class VideoSource:
    def __init__(self, config: SourceConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.capture: cv2.VideoCapture | None = None

    def _resolve_target(self) -> Any:
        if self.config.type == "camera":
            return int(self.config.camera_index)
        if self.config.type == "rtsp":
            if not self.config.rtsp_url:
                raise ValueError("RTSP source selected but `rtsp_url` is empty")
            return self.config.rtsp_url
        if self.config.type == "video_file":
            if not self.config.video_path:
                raise ValueError("Video file source selected but `video_path` is empty")
            video_path = Path(self.config.video_path)
            if not video_path.exists():
                raise FileNotFoundError(f"Video file does not exist: {video_path}")
            return str(video_path)
        if self.config.type == "capture":
            if not self.config.capture_uri:
                raise ValueError("Generic capture source selected but `capture_uri` is empty")
            return self.config.capture_uri
        raise ValueError(f"Unsupported source type: {self.config.type}")

    def open(self) -> None:
        target = self._resolve_target()
        api_preference = CAPTURE_API_MAP.get(self.config.api_preference.lower())
        if api_preference is None:
            capture = cv2.VideoCapture(target)
        else:
            capture = cv2.VideoCapture(target, api_preference)

        if not capture or not capture.isOpened():
            raise RuntimeError(f"Unable to open video source: {target}")

        if self.config.width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        if self.config.height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        if self.config.fps:
            capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, self.config.buffer_size)

        self.capture = capture
        self.logger.info("Video source opened: %s", self.describe())

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.capture is None:
            return False, None

        ok, frame = self.capture.read()
        if ok:
            frame = _rotate_frame(frame, self.config.rotation_deg)
            return True, frame

        if self.config.type == "video_file" and self.config.loop_video:
            self.logger.info("Video file reached EOF, reopening because loop_video=true")
            self.reconnect()
            if self.capture is None:
                return False, None
            ok, frame = self.capture.read()
            if ok and frame is not None:
                frame = _rotate_frame(frame, self.config.rotation_deg)
            return ok, frame
        return False, None

    def reconnect(self) -> None:
        self.release()
        self.open()

    def release(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def is_opened(self) -> bool:
        return bool(self.capture is not None and self.capture.isOpened())

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.config.type,
            "camera_index": self.config.camera_index,
            "rtsp_url": self.config.rtsp_url,
            "video_path": self.config.video_path,
            "capture_uri": self.config.capture_uri,
            "width": self.config.width,
            "height": self.config.height,
            "fps": self.config.fps,
            "rotation_deg": self.config.rotation_deg,
        }
