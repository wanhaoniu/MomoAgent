from __future__ import annotations

from face_tracking.config import HintConfig


class MirrorFollowControllerHint:
    def __init__(self, config: HintConfig) -> None:
        self.config = config

    def compute(
        self,
        raw_offset: dict[str, float],
        smoothed_offset: dict[str, float],
        raw_area_ratio: float,
        smoothed_area_ratio: float,
        detected: bool,
    ) -> dict[str, object]:
        if not detected:
            return {
                "distance_hint": "HOLD",
                "lateral_hint": "HOLD",
                "vertical_hint": "HOLD",
                "combined_hint": ["HOLD"],
            }

        offset = smoothed_offset if self.config.use_smoothed_offset else raw_offset
        area_ratio = smoothed_area_ratio if self.config.use_smoothed_area_ratio else raw_area_ratio

        ndx = float(offset["ndx"])
        ndy = float(offset["ndy"])

        if abs(ndx) <= self.config.dead_zone_ndx:
            lateral = "HOLD"
        else:
            lateral = "RIGHT" if ndx > 0 else "LEFT"

        if abs(ndy) <= self.config.dead_zone_ndy:
            vertical = "HOLD"
        else:
            vertical = "DOWN" if ndy > 0 else "UP"

        if area_ratio < self.config.min_face_area_ratio:
            distance = "FORWARD"
        elif area_ratio > self.config.max_face_area_ratio:
            distance = "BACKWARD"
        else:
            distance = "HOLD"

        combined = [item for item in [lateral, vertical, distance] if item != "HOLD"]
        if not combined:
            combined = ["HOLD"]

        return {
            "distance_hint": distance,
            "lateral_hint": lateral,
            "vertical_hint": vertical,
            "combined_hint": combined,
        }
