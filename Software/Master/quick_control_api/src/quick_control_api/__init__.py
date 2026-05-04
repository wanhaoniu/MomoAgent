from __future__ import annotations

import importlib
import sys
import warnings
from pathlib import Path

_SERVICE_SRC = Path(__file__).resolve().parents[3] / "momo_robot_service" / "src"
if str(_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVICE_SRC))

warnings.warn(
    "Package quick_control_api is deprecated; import momo_robot_service instead.",
    DeprecationWarning,
    stacklevel=2,
)

_MODULES = (
    "agent_service",
    "app",
    "attention_worker",
    "aws_transcribe_realtime",
    "errors",
    "face_follow_worker",
    "haiguitang_agent",
    "haiguitang_worker",
    "idle_scan_worker",
    "remote_tts",
    "scene_config",
    "schemas",
    "service",
)

for _name in _MODULES:
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"momo_robot_service.{_name}")

_impl = importlib.import_module("momo_robot_service")
for _key in getattr(_impl, "__all__", ()):
    globals()[_key] = getattr(_impl, _key)

__all__ = list(getattr(_impl, "__all__", ()))
