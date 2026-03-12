from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from face_tracking.detectors.base import FaceDetector
from face_tracking.schemas import FaceDetection


class YuNetFaceDetector(FaceDetector):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._detector = None
        self._haar_classifier = None
        self._haar_cascade_path = ""
        self._using_haar_fallback = False

    @property
    def backend_name(self) -> str:
        if self._using_haar_fallback:
            return "opencv_haar_fallback"
        return super().backend_name

    def _candidate_haar_paths(self) -> list[Path]:
        file_name = "haarcascade_frontalface_default.xml"
        candidates: list[Path] = []

        cv2_data = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if cv2_data:
            candidates.append(Path(cv2_data) / file_name)

        repo_root = Path(__file__).resolve().parents[3]
        candidates.extend((repo_root / ".venv" / "lib").glob(f"python*/site-packages/cv2/data/{file_name}"))

        for base in (
            Path("/usr/share/opencv4/haarcascades"),
            Path("/usr/share/opencv/haarcascades"),
            Path.home() / "miniconda3" / "envs",
            Path.home() / "anaconda3" / "envs",
        ):
            if base.name == "envs":
                candidates.extend(base.glob(f"*/lib/python*/site-packages/cv2/data/{file_name}"))
            else:
                candidates.append(base / file_name)

        deduped: list[Path] = []
        seen = set()
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _ensure_haar_fallback(self) -> bool:
        if self._haar_classifier is not None:
            return True

        for path in self._candidate_haar_paths():
            if not path.exists():
                continue
            classifier = cv2.CascadeClassifier(str(path))
            if classifier.empty():
                continue
            self._haar_classifier = classifier
            self._haar_cascade_path = str(path)
            return True
        return False

    def _detect_with_haar(self, frame: np.ndarray) -> list[FaceDetection]:
        if self._haar_classifier is None and not self._ensure_haar_fallback():
            raise RuntimeError("Haar cascade fallback is unavailable")

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._haar_classifier.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )

        detections: list[FaceDetection] = []
        for x, y, w, h in faces:
            detections.append(
                FaceDetection(
                    bbox=(float(x), float(y), float(x + w), float(y + h)),
                    confidence=1.0,
                    landmarks=None,
                )
            )
        return detections

    def initialize(self) -> None:
        model_path = self.config.model_path
        if not model_path:
            if self._ensure_haar_fallback():
                self._using_haar_fallback = True
                self.logger.warning("YuNet model_path is empty, fallback to Haar cascade: %s", self._haar_cascade_path)
                return
            raise RuntimeError("OpenCV YuNet backend requires `model_path`")
        if not Path(model_path).exists():
            if self._ensure_haar_fallback():
                self._using_haar_fallback = True
                self.logger.warning(
                    "YuNet model file is missing (%s), fallback to Haar cascade: %s",
                    model_path,
                    self._haar_cascade_path,
                )
                return
            raise FileNotFoundError(f"YuNet model file does not exist: {model_path}")

        input_size = tuple(self.config.yunet_input_size)
        max_faces = self.config.max_faces if self.config.max_faces > 0 else 5000

        try:
            if hasattr(cv2, "FaceDetectorYN_create"):
                self._detector = cv2.FaceDetectorYN_create(
                    model_path,
                    "",
                    input_size,
                    self.config.confidence_threshold,
                    self.config.nms_threshold,
                    max_faces,
                )
            elif hasattr(cv2, "FaceDetectorYN"):
                self._detector = cv2.FaceDetectorYN.create(
                    model_path,
                    "",
                    input_size,
                    self.config.confidence_threshold,
                    self.config.nms_threshold,
                    max_faces,
                )
            else:
                raise RuntimeError("Current OpenCV build does not expose FaceDetectorYN / YuNet support")
        except Exception as exc:
            if self._ensure_haar_fallback():
                self._using_haar_fallback = True
                self.logger.warning("YuNet init failed (%s), fallback to Haar cascade: %s", exc, self._haar_cascade_path)
                return
            raise

        self.logger.info("OpenCV YuNet detector initialized: %s", self.describe())

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        if self._using_haar_fallback:
            return self._detect_with_haar(frame)
        if self._detector is None:
            raise RuntimeError("Detector is not initialized")

        frame_height, frame_width = frame.shape[:2]
        self._detector.setInputSize((frame_width, frame_height))
        try:
            _, faces = self._detector.detect(frame)
        except cv2.error as exc:
            if self._ensure_haar_fallback():
                self._using_haar_fallback = True
                self.logger.warning("YuNet detect failed (%s), fallback to Haar cascade: %s", exc, self._haar_cascade_path)
                return self._detect_with_haar(frame)
            raise

        if faces is None or len(faces) == 0:
            return []

        detections: list[FaceDetection] = []
        for row in faces:
            x, y, w, h = row[:4]
            confidence = float(row[-1])
            if confidence < self.config.confidence_threshold:
                continue
            landmarks = []
            for idx in range(4, 14, 2):
                landmarks.append((float(row[idx]), float(row[idx + 1])))
            detections.append(
                FaceDetection(
                    bbox=(float(x), float(y), float(x + w), float(y + h)),
                    confidence=confidence,
                    landmarks=landmarks,
                )
            )
        return detections

    def describe(self) -> dict[str, object]:
        payload = super().describe()
        payload["fallback_active"] = self._using_haar_fallback
        payload["fallback_cascade_path"] = self._haar_cascade_path or None
        return payload
