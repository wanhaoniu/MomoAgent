from __future__ import annotations

from typing import Any


# Edit these two constants to retune the point the tracker tries to keep a face on.
TARGET_CENTER_X_NORM = 0.50
TARGET_CENTER_Y_NORM = 0.42


def _clamp_norm(value: Any) -> float:
    return min(1.0, max(0.0, float(value)))


def get_target_center_norm() -> tuple[float, float]:
    return (_clamp_norm(TARGET_CENTER_X_NORM), _clamp_norm(TARGET_CENTER_Y_NORM))


def resolve_target_center(frame_size: tuple[int, int]) -> tuple[float, float]:
    frame_width, frame_height = frame_size
    target_x_norm, target_y_norm = get_target_center_norm()
    return (float(frame_width) * target_x_norm, float(frame_height) * target_y_norm)


def build_target_center_payload(frame_size: tuple[int, int]) -> dict[str, float]:
    target_x, target_y = resolve_target_center(frame_size)
    target_x_norm, target_y_norm = get_target_center_norm()
    return {
        "x": round(target_x, 3),
        "y": round(target_y, 3),
        "x_norm": round(target_x_norm, 6),
        "y_norm": round(target_y_norm, 6),
    }
