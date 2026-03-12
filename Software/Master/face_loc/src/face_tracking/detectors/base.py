from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from face_tracking.config import DetectorConfig
from face_tracking.schemas import FaceDetection


@dataclass(slots=True)
class RuntimeDeviceInfo:
    requested: str
    resolved: str
    providers: list[str]
    ctx_id: int


def resolve_runtime_device(device_preference: str, logger: logging.Logger) -> RuntimeDeviceInfo:
    try:
        import onnxruntime as ort

        available_providers = ort.get_available_providers()
    except Exception as exc:
        logger.warning("Unable to inspect onnxruntime providers, fallback to CPU only: %s", exc)
        available_providers = ["CPUExecutionProvider"]

    cuda_available = "CUDAExecutionProvider" in available_providers
    resolved = "cpu"
    ctx_id = -1
    providers = ["CPUExecutionProvider"]

    if device_preference == "cuda":
        if not cuda_available:
            logger.warning("CUDA requested but CUDAExecutionProvider is unavailable, fallback to CPU")
        else:
            resolved = "cuda"
    elif device_preference == "auto" and cuda_available:
        resolved = "cuda"

    if resolved == "cuda":
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        ctx_id = 0

    return RuntimeDeviceInfo(
        requested=device_preference,
        resolved=resolved,
        providers=providers,
        ctx_id=ctx_id,
    )


class FaceDetector(ABC):
    def __init__(self, config: DetectorConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        if config.backend.startswith("insightface"):
            self.runtime = resolve_runtime_device(config.device, self.logger)
        else:
            self.runtime = RuntimeDeviceInfo(
                requested=config.device,
                resolved="cpu",
                providers=["CPUExecutionProvider"],
                ctx_id=-1,
            )

    @property
    def backend_name(self) -> str:
        return self.config.backend

    @abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def describe(self) -> dict[str, object]:
        return {
            "backend": self.backend_name,
            "requested_device": self.runtime.requested,
            "device": self.runtime.resolved,
            "providers": self.runtime.providers,
            "model_path": self.config.model_path,
            "model_name": self.config.model_name,
        }
