from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


class SourceConfig(BaseModel):
    type: Literal["camera", "capture"] = "camera"
    camera_index: int = 0
    capture_uri: str | None = None
    width: int | None = 1280
    height: int | None = 720
    fps: float | None = 30.0
    rotation_deg: int = 0
    api_preference: str = "auto"


class RecognizerConfig(BaseModel):
    model_path: str = "../weights/gesture_recognizer.task"
    model_url: str = (
        "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
        "gesture_recognizer/float16/1/gesture_recognizer.task"
    )
    allow_auto_download: bool = True
    num_hands: int = Field(default=1, ge=1, le=4)
    min_hand_detection_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    min_hand_presence_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    min_tracking_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    stability_frames: int = Field(default=4, ge=1, le=30)
    missing_reset_frames: int = Field(default=4, ge=1, le=120)


class VisualizerConfig(BaseModel):
    enabled: bool = False
    window_name: str = "MediaPipe Gesture Tracking"
    draw_landmarks: bool = True


class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8012


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "./logs"
    file_name: str = "gesture_tracking.log"


class AppConfig(BaseModel):
    app_name: str = "smart-mirror-gesture-tracking"
    source: SourceConfig = Field(default_factory=SourceConfig)
    recognizer: RecognizerConfig = Field(default_factory=RecognizerConfig)
    visualizer: VisualizerConfig = Field(default_factory=VisualizerConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


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
    config.source.capture_uri = _resolve_path(config.source.capture_uri, base_dir)
    config.recognizer.model_path = _resolve_path(config.recognizer.model_path, base_dir) or config.recognizer.model_path
    config.logging.log_dir = _resolve_path(config.logging.log_dir, base_dir) or config.logging.log_dir
    return config
