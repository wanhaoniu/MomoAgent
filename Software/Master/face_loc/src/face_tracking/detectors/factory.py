from __future__ import annotations

import logging

from face_tracking.config import DetectorConfig
from face_tracking.detectors.base import FaceDetector
from face_tracking.detectors.insightface_backend import InsightFaceDetector
from face_tracking.detectors.yunet_backend import YuNetFaceDetector


def create_detector(config: DetectorConfig, logger: logging.Logger | None = None) -> FaceDetector:
    backend = config.backend
    if backend in {"insightface_onnx", "insightface_faceanalysis"}:
        return InsightFaceDetector(config=config, logger=logger)
    if backend == "opencv_yunet":
        return YuNetFaceDetector(config=config, logger=logger)
    raise ValueError(f"Unsupported detector backend: {backend}")
