from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_HAIGUITANG_SCENE_CONFIG_PATH = (
    REPO_ROOT / "Software" / "Master" / "quick_control_api" / "runtime" / "haiguitang_scene.json"
)
DEFAULT_HAIGUITANG_MEDIA_DIR = (
    REPO_ROOT / "Software" / "Master" / "quick_control_api" / "runtime" / "media"
)
DEFAULT_HAIGUITANG_INTRO_VIDEO_FILE = DEFAULT_HAIGUITANG_MEDIA_DIR / "haiguitang_intro.mp4"
DEFAULT_HAIGUITANG_INTRO_VIDEO_ROUTE = "/api/v1/scenes/haiguitang/intro-video"


def _read_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _read_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return min(max(value, minimum), maximum)


@dataclass
class HaiGuiTangSceneConfig:
    scene_id: str = "haiguitang"
    title: str = "海龟汤"
    subtitle: str = "片头结束后进入互动模式"
    intro_video_url: str = ""
    intro_video_auto_play: bool = True
    intro_video_skipable: bool = True
    intro_video_timeout_sec: float = 8.0
    default_status_text: str = "进入互动区后，可以先用点头和摇头调试动作效果。"
    placeholder_title: str = "片头占位"
    placeholder_body: str = "当前还没有正式视频素材，后续把 mp4 放到固定路径或改配置 URL 就能替换。"
    media_file_path: str = str(DEFAULT_HAIGUITANG_INTRO_VIDEO_FILE)
    media_route_path: str = DEFAULT_HAIGUITANG_INTRO_VIDEO_ROUTE


def haiguitang_intro_video_file() -> Path:
    return DEFAULT_HAIGUITANG_INTRO_VIDEO_FILE


def _load_file_overrides(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _merge_dataclass_fields(
    config: HaiGuiTangSceneConfig,
    overrides: dict[str, Any],
) -> HaiGuiTangSceneConfig:
    fields = set(asdict(config).keys())
    updates = {
        key: value
        for key, value in overrides.items()
        if key in fields and value is not None
    }
    return HaiGuiTangSceneConfig(**{**asdict(config), **updates})


def load_haiguitang_scene_config() -> dict[str, Any]:
    config = HaiGuiTangSceneConfig()

    config_path_raw = str(
        os.getenv(
            "QUICK_CONTROL_HAIGUITANG_SCENE_CONFIG_PATH",
            str(DEFAULT_HAIGUITANG_SCENE_CONFIG_PATH),
        )
        or ""
    ).strip()
    config_path = Path(config_path_raw).expanduser() if config_path_raw else DEFAULT_HAIGUITANG_SCENE_CONFIG_PATH
    config = _merge_dataclass_fields(config, _load_file_overrides(config_path))

    intro_video_url = str(
        os.getenv(
            "QUICK_CONTROL_HAIGUITANG_INTRO_VIDEO_URL",
            config.intro_video_url,
        )
        or ""
    ).strip()

    if not intro_video_url and haiguitang_intro_video_file().is_file():
        intro_video_url = DEFAULT_HAIGUITANG_INTRO_VIDEO_ROUTE

    config = HaiGuiTangSceneConfig(
        **{
            **asdict(config),
            "title": str(
                os.getenv("QUICK_CONTROL_HAIGUITANG_TITLE", config.title) or ""
            ).strip()
            or config.title,
            "subtitle": str(
                os.getenv("QUICK_CONTROL_HAIGUITANG_SUBTITLE", config.subtitle) or ""
            ).strip()
            or config.subtitle,
            "intro_video_url": intro_video_url,
            "intro_video_auto_play": _read_bool(
                "QUICK_CONTROL_HAIGUITANG_INTRO_VIDEO_AUTO_PLAY",
                config.intro_video_auto_play,
            ),
            "intro_video_skipable": _read_bool(
                "QUICK_CONTROL_HAIGUITANG_INTRO_VIDEO_SKIPABLE",
                config.intro_video_skipable,
            ),
            "intro_video_timeout_sec": _read_float(
                "QUICK_CONTROL_HAIGUITANG_INTRO_VIDEO_TIMEOUT_SEC",
                config.intro_video_timeout_sec,
                minimum=1.0,
                maximum=60.0,
            ),
            "default_status_text": str(
                os.getenv(
                    "QUICK_CONTROL_HAIGUITANG_DEFAULT_STATUS_TEXT",
                    config.default_status_text,
                )
                or ""
            ).strip()
            or config.default_status_text,
            "placeholder_title": str(
                os.getenv(
                    "QUICK_CONTROL_HAIGUITANG_PLACEHOLDER_TITLE",
                    config.placeholder_title,
                )
                or ""
            ).strip()
            or config.placeholder_title,
            "placeholder_body": str(
                os.getenv(
                    "QUICK_CONTROL_HAIGUITANG_PLACEHOLDER_BODY",
                    config.placeholder_body,
                )
                or ""
            ).strip()
            or config.placeholder_body,
            "media_file_path": str(haiguitang_intro_video_file()),
            "media_route_path": DEFAULT_HAIGUITANG_INTRO_VIDEO_ROUTE,
        }
    )
    return asdict(config)


__all__ = [
    "DEFAULT_HAIGUITANG_INTRO_VIDEO_ROUTE",
    "HaiGuiTangSceneConfig",
    "haiguitang_intro_video_file",
    "load_haiguitang_scene_config",
]
