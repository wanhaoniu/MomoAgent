from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


class SourceConfig(BaseModel):
    type: Literal["camera", "rtsp", "video_file", "capture"] = "camera"
    camera_index: int = 0
    camera_name: str | None = None
    rtsp_url: str | None = None
    video_path: str | None = None
    capture_uri: str | None = None
    width: int | None = 1280
    height: int | None = 720
    fps: float | None = 30.0
    rotation_deg: int = 0
    buffer_size: int = 2
    reconnect_interval_sec: float = 2.0
    open_timeout_sec: float = 10.0
    loop_video: bool = False
    api_preference: str = "auto"


class DetectorConfig(BaseModel):
    backend: Literal["insightface_onnx", "insightface_faceanalysis", "opencv_yunet"] = "insightface_onnx"
    model_path: str | None = None
    model_name: str | None = None
    model_root: str = "./weights/insightface"
    allow_auto_download: bool = False
    input_size: tuple[int, int] = (640, 640)
    confidence_threshold: float = 0.6
    nms_threshold: float = 0.4
    max_faces: int = 0
    device: Literal["auto", "cpu", "cuda"] = "auto"
    yunet_input_size: tuple[int, int] = (320, 320)


class SelectionConfig(BaseModel):
    strategy: Literal["largest_face", "highest_confidence", "closest_to_center"] = "largest_face"


class SmoothingConfig(BaseModel):
    enabled: bool = True
    alpha_center: float = 0.35
    alpha_area: float = 0.25
    max_missing_frames_before_reset: int = 5


class HintConfig(BaseModel):
    dead_zone_ndx: float = 0.08
    dead_zone_ndy: float = 0.08
    min_face_area_ratio: float = 0.10
    max_face_area_ratio: float = 0.28
    use_smoothed_offset: bool = True
    use_smoothed_area_ratio: bool = True


class VisualizerConfig(BaseModel):
    enabled: bool = True
    window_name: str = "Smart Mirror Face Tracking"
    draw_landmarks: bool = True


class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    websocket_path: str = "/ws/stream"
    ws_interval_sec: float = 0.05


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "./logs"
    file_name: str = "face_tracking.log"


class RuntimeConfig(BaseModel):
    frame_queue_size: int = Field(default=2, ge=1)
    fps_window_size: int = Field(default=30, ge=2)


class AppConfig(BaseModel):
    app_name: str = "smart-mirror-face-tracking"
    source: SourceConfig = Field(default_factory=SourceConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    smoothing: SmoothingConfig = Field(default_factory=SmoothingConfig)
    hint: HintConfig = Field(default_factory=HintConfig)
    visualizer: VisualizerConfig = Field(default_factory=VisualizerConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


def _expand_env_placeholders(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2) or ""
        return os.getenv(key, default)

    return ENV_PATTERN.sub(replace, text)


def _resolve_path(value: str | None, base_dir: Path) -> str | None:
    if not value:
        return value
    if value.startswith(("rtsp://", "http://", "https://", "file://")):
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str((base_dir / candidate).resolve())


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    expanded_text = _expand_env_placeholders(raw_text)
    data = yaml.safe_load(expanded_text) or {}
    config = AppConfig.model_validate(data)

    base_dir = path.parent
    config.source.video_path = _resolve_path(config.source.video_path, base_dir)
    config.detector.model_path = _resolve_path(config.detector.model_path, base_dir)
    config.detector.model_root = _resolve_path(config.detector.model_root, base_dir) or config.detector.model_root
    config.logging.log_dir = _resolve_path(config.logging.log_dir, base_dir) or config.logging.log_dir
    return config
