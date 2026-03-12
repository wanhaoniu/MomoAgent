from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from face_tracking.detectors.base import FaceDetector
from face_tracking.schemas import FaceDetection


class InsightFaceDetector(FaceDetector):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._detector_model: Any | None = None
        self._face_analysis_app: Any | None = None

    def initialize(self) -> None:
        if self.config.backend == "insightface_faceanalysis":
            self._initialize_face_analysis()
            return
        self._initialize_onnx_model()

    def _initialize_onnx_model(self) -> None:
        try:
            from insightface.model_zoo import get_model
        except Exception as exc:
            raise RuntimeError(
                "Failed to import insightface.model_zoo. Please install insightface and onnxruntime first."
            ) from exc

        model_ref = self.config.model_path or self.config.model_name
        if not model_ref:
            raise RuntimeError("InsightFace ONNX backend requires `model_path` or `model_name`")

        if self.config.model_path:
            model_path = Path(self.config.model_path)
            if not model_path.exists():
                raise FileNotFoundError(f"Model weight file does not exist: {model_path}")

        kwargs = {
            "root": self.config.model_root,
            "download": self.config.allow_auto_download,
            "providers": self.runtime.providers,
        }
        try:
            self._detector_model = get_model(model_ref, **kwargs)
        except TypeError:
            kwargs.pop("providers", None)
            self._detector_model = get_model(model_ref, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"Failed to load detector model from {model_ref}: {exc}") from exc

        prepare_kwargs = {
            "ctx_id": self.runtime.ctx_id,
            "input_size": tuple(self.config.input_size),
            "det_thresh": self.config.confidence_threshold,
            "nms_thresh": self.config.nms_threshold,
        }
        try:
            self._detector_model.prepare(**prepare_kwargs)
        except TypeError:
            fallback_kwargs = {
                "ctx_id": self.runtime.ctx_id,
                "det_thresh": self.config.confidence_threshold,
            }
            self._detector_model.prepare(**fallback_kwargs)

        self.logger.info("InsightFace ONNX detector initialized: %s", self.describe())

    def _initialize_face_analysis(self) -> None:
        try:
            from insightface.app import FaceAnalysis
        except Exception as exc:
            raise RuntimeError(
                "Failed to import insightface.app.FaceAnalysis. Please install insightface and onnxruntime first."
            ) from exc

        model_name = self.config.model_name or "buffalo_l"
        if not self.config.allow_auto_download:
            candidate_dirs = [
                Path(self.config.model_root) / "models" / model_name,
                Path(self.config.model_root) / model_name,
            ]
            if not any(candidate.exists() for candidate in candidate_dirs):
                raise FileNotFoundError(
                    "FaceAnalysis model pack not found locally. "
                    f"Expected one of: {candidate_dirs}. "
                    "Either place the pack locally or set allow_auto_download=true."
                )

        kwargs = {
            "name": model_name,
            "root": self.config.model_root,
            "providers": self.runtime.providers,
            "allowed_modules": ["detection"],
        }
        try:
            self._face_analysis_app = FaceAnalysis(**kwargs)
        except TypeError:
            kwargs.pop("allowed_modules", None)
            try:
                self._face_analysis_app = FaceAnalysis(**kwargs)
            except TypeError:
                kwargs.pop("providers", None)
                self._face_analysis_app = FaceAnalysis(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize FaceAnalysis with model pack {model_name}: {exc}") from exc

        self._face_analysis_app.prepare(
            ctx_id=self.runtime.ctx_id,
            det_size=tuple(self.config.input_size),
            det_thresh=self.config.confidence_threshold,
        )
        self.logger.info("InsightFace FaceAnalysis detector initialized: %s", self.describe())

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        if self._detector_model is None and self._face_analysis_app is None:
            raise RuntimeError("Detector is not initialized")

        if self._detector_model is not None:
            bboxes, kpss = self._detector_model.detect(
                frame,
                input_size=tuple(self.config.input_size),
                max_num=self.config.max_faces,
            )
            if bboxes is None or len(bboxes) == 0:
                return []

            detections: list[FaceDetection] = []
            for index, bbox in enumerate(bboxes):
                score = float(bbox[4])
                if score < self.config.confidence_threshold:
                    continue
                landmarks = None
                if kpss is not None and len(kpss) > index:
                    landmarks = [(float(x), float(y)) for x, y in np.asarray(kpss[index]).tolist()]
                detections.append(
                    FaceDetection(
                        bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                        confidence=score,
                        landmarks=landmarks,
                    )
                )
            return detections

        faces = self._face_analysis_app.get(frame)
        detections = []
        for face in faces:
            bbox = np.asarray(face.bbox).tolist()
            score = float(getattr(face, "det_score", 0.0))
            if score < self.config.confidence_threshold:
                continue
            landmarks = None
            if getattr(face, "kps", None) is not None:
                landmarks = [(float(x), float(y)) for x, y in np.asarray(face.kps).tolist()]
            detections.append(
                FaceDetection(
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    confidence=score,
                    landmarks=landmarks,
                )
            )

        if self.config.max_faces > 0:
            detections = detections[: self.config.max_faces]
        return detections
