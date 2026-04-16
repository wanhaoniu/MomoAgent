from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_IDLE_SCAN_PAN_JOINT = "shoulder_pan"
DEFAULT_IDLE_SCAN_TILT_JOINT = "elbow_flex"
DEFAULT_IDLE_SCAN_REFRAME_JOINT = "shoulder_lift"
DEFAULT_IDLE_SCAN_SPEED_PERCENT = 18
DEFAULT_IDLE_SCAN_PAN_RANGE_DEG = 30.0
DEFAULT_IDLE_SCAN_TILT_RANGE_DEG = 16.0
DEFAULT_IDLE_SCAN_REFRAME_RANGE_DEG = 5.0
DEFAULT_IDLE_SCAN_MOVE_DURATION_MIN_SEC = 1.8
DEFAULT_IDLE_SCAN_MOVE_DURATION_MAX_SEC = 3.8
DEFAULT_IDLE_SCAN_DWELL_SEC_MIN = 0.8
DEFAULT_IDLE_SCAN_DWELL_SEC_MAX = 2.5
DEFAULT_IDLE_SCAN_POLL_INTERVAL_SEC = 0.10


@dataclass
class IdleScanConfig:
    pan_joint: str = DEFAULT_IDLE_SCAN_PAN_JOINT
    tilt_joint: str = DEFAULT_IDLE_SCAN_TILT_JOINT
    reframe_joint: str | None = DEFAULT_IDLE_SCAN_REFRAME_JOINT
    speed_percent: int = DEFAULT_IDLE_SCAN_SPEED_PERCENT
    pan_range_deg: float = DEFAULT_IDLE_SCAN_PAN_RANGE_DEG
    tilt_range_deg: float = DEFAULT_IDLE_SCAN_TILT_RANGE_DEG
    reframe_range_deg: float = DEFAULT_IDLE_SCAN_REFRAME_RANGE_DEG
    move_duration_min_sec: float = DEFAULT_IDLE_SCAN_MOVE_DURATION_MIN_SEC
    move_duration_max_sec: float = DEFAULT_IDLE_SCAN_MOVE_DURATION_MAX_SEC
    dwell_sec_min: float = DEFAULT_IDLE_SCAN_DWELL_SEC_MIN
    dwell_sec_max: float = DEFAULT_IDLE_SCAN_DWELL_SEC_MAX
    poll_interval_sec: float = DEFAULT_IDLE_SCAN_POLL_INTERVAL_SEC


@dataclass
class IdleScanState:
    enabled: bool = False
    phase: str = "none"
    phase_deadline_monotonic: float = 0.0
    anchor_pan_deg: float | None = None
    anchor_tilt_deg: float | None = None
    anchor_reframe_deg: float | None = None
    current_target_pan_deg: float | None = None
    current_target_tilt_deg: float | None = None
    current_target_reframe_deg: float | None = None
    shot_name: str = "none"
    micro_adjust_remaining: int = 0
    last_command_duration_sec: float | None = None


def build_default_idle_scan_payload(config: IdleScanConfig | None = None) -> dict[str, Any]:
    cfg = config or IdleScanConfig()
    return {
        "enabled": False,
        "running": False,
        "behavior_mode": "idle_scan",
        "phase": "none",
        "pan_joint": str(cfg.pan_joint),
        "tilt_joint": str(cfg.tilt_joint),
        "reframe_joint": str(cfg.reframe_joint or "").strip() or None,
        "speed_percent": int(cfg.speed_percent),
        "pan_range_deg": float(cfg.pan_range_deg),
        "tilt_range_deg": float(cfg.tilt_range_deg),
        "reframe_range_deg": float(cfg.reframe_range_deg),
        "move_duration_min_sec": float(cfg.move_duration_min_sec),
        "move_duration_max_sec": float(cfg.move_duration_max_sec),
        "dwell_sec_min": float(cfg.dwell_sec_min),
        "dwell_sec_max": float(cfg.dwell_sec_max),
        "poll_interval_sec": float(cfg.poll_interval_sec),
        "anchor_pan_deg": None,
        "anchor_tilt_deg": None,
        "anchor_reframe_deg": None,
        "current_target_pan_deg": None,
        "current_target_tilt_deg": None,
        "current_target_reframe_deg": None,
        "shot_name": "none",
        "micro_adjust_remaining": 0,
        "last_command_duration_sec": None,
        "dwell_remaining_sec": None,
        "last_error": "",
        "started_at": None,
        "config": asdict(cfg),
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


def _joint_limits_deg_map(robot: Any) -> dict[str, tuple[float, float]]:
    robot_model = getattr(robot, "robot_model", None)
    joint_names = list(getattr(robot_model, "joint_names", []) or [])
    joint_limits = list(getattr(robot_model, "joint_limits", []) or [])
    limit_map: dict[str, tuple[float, float]] = {}
    for joint_name, limit_pair in zip(joint_names, joint_limits):
        try:
            lower_rad, upper_rad = limit_pair
            limit_map[str(joint_name)] = (
                float(math.degrees(float(lower_rad))),
                float(math.degrees(float(upper_rad))),
            )
        except Exception:
            continue
    return limit_map


def clamp_targets_deg(robot: Any, targets_deg: dict[str, float]) -> dict[str, float]:
    limit_map = _joint_limits_deg_map(robot)
    clamped: dict[str, float] = {}
    for joint_name, target_deg in targets_deg.items():
        if joint_name in limit_map:
            lower_deg, upper_deg = limit_map[joint_name]
            clamped[joint_name] = _clamp(target_deg, lower_deg, upper_deg)
        else:
            clamped[joint_name] = float(target_deg)
    return clamped


def _normalize_optional_joint_name(joint_name: str | None) -> str | None:
    normalized = str(joint_name or "").strip()
    return normalized or None


_SHOT_TEMPLATES: tuple[tuple[str, float, float, float, float], ...] = (
    ("center", 0.0, 0.0, 0.0, 0.85),
    ("left", -0.92, 0.08, 0.08, 1.05),
    ("right", 0.92, 0.08, 0.08, 1.05),
    ("left_high", -0.74, 0.64, 0.10, 1.05),
    ("right_high", 0.74, 0.64, 0.10, 1.05),
    ("high", 0.0, 0.86, 0.12, 0.95),
    ("low", 0.0, -0.62, -0.04, 0.90),
    ("close", 0.0, 0.22, -0.55, 0.35),
    ("open_left", -0.58, 0.08, 0.48, 0.42),
    ("open_right", 0.58, 0.08, 0.48, 0.42),
)

_LATERAL_SHOT_MIN_SHIFT_FACTOR = {
    "left": 0.50,
    "right": 0.50,
    "left_high": 0.42,
    "right_high": 0.42,
    "open_left": 0.34,
    "open_right": 0.34,
}
_VERTICAL_SHOT_MIN_SHIFT_FACTOR = {
    "left_high": 0.34,
    "right_high": 0.34,
    "high": 0.44,
    "low": 0.32,
}
_DEFAULT_IDLE_SCAN_INTERPOLATION_HZ = 40.0
_IDLE_SCAN_REVERSE_PAN_COMPENSATION_MIN_DELTA_DEG = 6.0
_IDLE_SCAN_REVERSE_PAN_LAUNCH_GAIN = 0.70
_IDLE_SCAN_REVERSE_PAN_LAUNCH_PORTION = 0.35


def _smoothstep(alpha: float) -> float:
    t = _clamp(alpha, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _should_apply_reverse_pan_compensation(total_pan_delta_deg: float) -> bool:
    return float(total_pan_delta_deg) <= -abs(float(_IDLE_SCAN_REVERSE_PAN_COMPENSATION_MIN_DELTA_DEG))


def _reverse_pan_launch_alpha(progress: float) -> float:
    t = _clamp(progress, 0.0, 1.0)
    base_alpha = _smoothstep(t)
    launch_portion = _clamp(float(_IDLE_SCAN_REVERSE_PAN_LAUNCH_PORTION), 0.10, 0.60)
    if t <= 0.0 or t >= launch_portion:
        return base_alpha
    launch_gain = _clamp(float(_IDLE_SCAN_REVERSE_PAN_LAUNCH_GAIN), 0.0, 0.95)
    blend = launch_gain * (1.0 - t / launch_portion)
    ease_out_alpha = math.sin(t * math.pi * 0.5)
    adjusted_alpha = base_alpha + (ease_out_alpha - base_alpha) * blend
    return _clamp(adjusted_alpha, base_alpha, 1.0)


class IdleScanPlanner:
    def __init__(self, config: IdleScanConfig, *, rng: random.Random | None = None) -> None:
        self._config = config
        self._state = IdleScanState()
        self._rng = rng or random.Random()
        self._reframe_joint = _normalize_optional_joint_name(config.reframe_joint)

    @property
    def state(self) -> IdleScanState:
        return self._state

    def _has_reframe_joint(self) -> bool:
        return self._reframe_joint is not None

    def _duration_with_speed_bias(self, lower: float, upper: float) -> float:
        low = float(min(lower, upper))
        high = float(max(lower, upper))
        if high <= low + 1e-9:
            return low
        speed_ratio = _clamp((float(self._config.speed_percent) - 1.0) / 99.0, 0.0, 1.0)
        mode = high - (high - low) * float(speed_ratio)
        return float(self._rng.triangular(low, high, mode))

    def _sample_observe_duration(self) -> float:
        return self._duration_with_speed_bias(
            max(0.40, float(self._config.dwell_sec_min) * 0.65),
            max(0.75, float(self._config.dwell_sec_max) * 0.85),
        )

    def _sample_linger_duration(self) -> float:
        return self._duration_with_speed_bias(
            max(0.65, float(self._config.dwell_sec_min) * 0.85),
            max(1.20, float(self._config.dwell_sec_max) * 1.05),
        )

    def _sample_settle_duration(self) -> float:
        return self._duration_with_speed_bias(0.22, 0.50)

    def _sample_main_move_duration(self) -> float:
        return self._duration_with_speed_bias(
            float(self._config.move_duration_min_sec),
            float(self._config.move_duration_max_sec),
        )

    def _sample_micro_move_duration(self) -> float:
        min_duration = max(0.28, float(self._config.move_duration_min_sec) * 0.18)
        max_duration = max(
            min_duration + 0.04,
            min(1.20, float(self._config.move_duration_max_sec) * 0.28),
        )
        return self._duration_with_speed_bias(min_duration, max_duration)

    def _move_extent_ratio(
        self,
        targets_deg: dict[str, float],
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        current_reframe_deg: float | None = None,
    ) -> float:
        ratios: list[float] = []
        pan_joint_name = str(self._config.pan_joint)
        tilt_joint_name = str(self._config.tilt_joint)

        if pan_joint_name in targets_deg:
            pan_range = max(1.0, abs(float(self._config.pan_range_deg)))
            ratios.append(abs(float(targets_deg[pan_joint_name]) - float(current_pan_deg)) / pan_range)
        if tilt_joint_name in targets_deg:
            tilt_range = max(1.0, abs(float(self._config.tilt_range_deg)))
            ratios.append(abs(float(targets_deg[tilt_joint_name]) - float(current_tilt_deg)) / tilt_range)
        if (
            self._has_reframe_joint()
            and current_reframe_deg is not None
            and str(self._reframe_joint) in targets_deg
        ):
            reframe_range = max(1.0, abs(float(self._config.reframe_range_deg)))
            ratios.append(
                abs(float(targets_deg[str(self._reframe_joint)]) - float(current_reframe_deg))
                / reframe_range
            )

        if not ratios:
            return 0.0
        return _clamp(max(ratios), 0.0, 1.6)

    def _main_move_duration_for_targets(
        self,
        targets_deg: dict[str, float],
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        current_reframe_deg: float | None = None,
    ) -> float:
        base_duration = self._sample_main_move_duration()
        move_ratio = self._move_extent_ratio(
            targets_deg,
            current_pan_deg=current_pan_deg,
            current_tilt_deg=current_tilt_deg,
            current_reframe_deg=current_reframe_deg,
        )
        scaled_duration = base_duration * (0.88 + move_ratio * 0.48)
        return _clamp(
            scaled_duration,
            max(1.00, float(self._config.move_duration_min_sec) * 0.85),
            max(
                float(self._config.move_duration_max_sec) * 1.18,
                float(self._config.move_duration_min_sec) + 0.6,
            ),
        )

    def _micro_move_duration_for_targets(
        self,
        targets_deg: dict[str, float],
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        current_reframe_deg: float | None = None,
    ) -> float:
        base_duration = self._sample_micro_move_duration()
        move_ratio = self._move_extent_ratio(
            targets_deg,
            current_pan_deg=current_pan_deg,
            current_tilt_deg=current_tilt_deg,
            current_reframe_deg=current_reframe_deg,
        )
        scaled_duration = base_duration * (0.90 + move_ratio * 0.26)
        return _clamp(scaled_duration, 0.25, 1.30)

    def _initial_micro_adjust_count(self, shot_name: str) -> int:
        if shot_name == "center":
            return 0
        return 1 if self._rng.random() < 0.62 else 0

    def _select_shot_template(self) -> tuple[str, float, float, float]:
        weights: list[float] = []
        for shot_name, _pan_factor, _tilt_factor, reframe_factor, base_weight in _SHOT_TEMPLATES:
            weight = float(base_weight)
            if shot_name == self._state.shot_name:
                weight *= 0.12
            if not self._has_reframe_joint() and abs(float(reframe_factor)) > 0.4:
                weight *= 0.65
            weights.append(max(0.01, weight))
        shot_name, pan_factor, tilt_factor, reframe_factor, _weight = self._rng.choices(
            list(_SHOT_TEMPLATES),
            weights=weights,
            k=1,
        )[0]
        return str(shot_name), float(pan_factor), float(tilt_factor), float(reframe_factor)

    def _compose_main_targets(self) -> tuple[str, dict[str, float]]:
        shot_name, pan_factor, tilt_factor, reframe_factor = self._select_shot_template()

        assert self._state.anchor_pan_deg is not None
        assert self._state.anchor_tilt_deg is not None

        desired_pan_deg = float(self._state.anchor_pan_deg) + float(self._config.pan_range_deg) * pan_factor
        desired_tilt_deg = float(self._state.anchor_tilt_deg) + float(self._config.tilt_range_deg) * tilt_factor

        if self._state.current_target_pan_deg is not None:
            pan_blend_lower = 0.72 if shot_name in _LATERAL_SHOT_MIN_SHIFT_FACTOR else 0.56
            pan_blend_upper = 0.96 if shot_name in _LATERAL_SHOT_MIN_SHIFT_FACTOR else 0.86
            blend = self._rng.uniform(pan_blend_lower, pan_blend_upper)
            desired_pan_deg = float(self._state.current_target_pan_deg) + (
                desired_pan_deg - float(self._state.current_target_pan_deg)
            ) * blend
        if self._state.current_target_tilt_deg is not None:
            if shot_name in _VERTICAL_SHOT_MIN_SHIFT_FACTOR:
                blend = self._rng.uniform(0.68, 0.94)
            else:
                blend = self._rng.uniform(0.48, 0.82)
            desired_tilt_deg = float(self._state.current_target_tilt_deg) + (
                desired_tilt_deg - float(self._state.current_target_tilt_deg)
            ) * blend

        desired_pan_deg += self._rng.uniform(
            -float(self._config.pan_range_deg) * 0.04,
            float(self._config.pan_range_deg) * 0.04,
        )
        desired_tilt_deg += self._rng.uniform(
            -float(self._config.tilt_range_deg) * 0.035,
            float(self._config.tilt_range_deg) * 0.035,
        )

        min_lateral_shift_factor = _LATERAL_SHOT_MIN_SHIFT_FACTOR.get(shot_name)
        if min_lateral_shift_factor is not None:
            reference_pan_deg = (
                float(self._state.current_target_pan_deg)
                if self._state.current_target_pan_deg is not None
                else float(self._state.anchor_pan_deg)
            )
            pan_delta = float(desired_pan_deg) - reference_pan_deg
            if abs(pan_delta) > 1e-9:
                direction = 1.0 if pan_delta >= 0.0 else -1.0
                min_pan_shift_deg = _clamp(
                    float(self._config.pan_range_deg) * float(min_lateral_shift_factor),
                    8.0,
                    20.0,
                )
                if abs(pan_delta) < min_pan_shift_deg:
                    desired_pan_deg = reference_pan_deg + direction * min_pan_shift_deg

        min_vertical_shift_factor = _VERTICAL_SHOT_MIN_SHIFT_FACTOR.get(shot_name)
        if min_vertical_shift_factor is not None:
            reference_tilt_deg = (
                float(self._state.current_target_tilt_deg)
                if self._state.current_target_tilt_deg is not None
                else float(self._state.anchor_tilt_deg)
            )
            tilt_delta = float(desired_tilt_deg) - reference_tilt_deg
            if abs(tilt_delta) > 1e-9:
                direction = 1.0 if tilt_delta >= 0.0 else -1.0
                min_tilt_shift_deg = _clamp(
                    float(self._config.tilt_range_deg) * float(min_vertical_shift_factor),
                    2.6,
                    9.0,
                )
                if abs(tilt_delta) < min_tilt_shift_deg:
                    desired_tilt_deg = reference_tilt_deg + direction * min_tilt_shift_deg

        targets_deg = {
            str(self._config.pan_joint): float(desired_pan_deg),
            str(self._config.tilt_joint): float(desired_tilt_deg),
        }

        desired_reframe_deg: float | None = None
        if self._has_reframe_joint() and self._state.anchor_reframe_deg is not None:
            desired_reframe_deg = float(self._state.anchor_reframe_deg) + float(self._config.reframe_range_deg) * reframe_factor
            if self._state.current_target_reframe_deg is not None:
                blend = self._rng.uniform(0.38, 0.70)
                desired_reframe_deg = float(self._state.current_target_reframe_deg) + (
                    desired_reframe_deg - float(self._state.current_target_reframe_deg)
                ) * blend
            desired_reframe_deg += self._rng.uniform(
                -float(self._config.reframe_range_deg) * 0.03,
                float(self._config.reframe_range_deg) * 0.03,
            )
            targets_deg[str(self._reframe_joint)] = float(desired_reframe_deg)

        self._state.shot_name = str(shot_name)
        self._state.current_target_pan_deg = float(desired_pan_deg)
        self._state.current_target_tilt_deg = float(desired_tilt_deg)
        self._state.current_target_reframe_deg = (
            float(desired_reframe_deg) if desired_reframe_deg is not None else None
        )
        self._state.micro_adjust_remaining = self._initial_micro_adjust_count(shot_name)
        return str(shot_name), targets_deg

    def _compose_micro_adjust_targets(self) -> dict[str, float]:
        assert self._state.current_target_pan_deg is not None
        assert self._state.current_target_tilt_deg is not None

        scale = 0.05 if self._state.micro_adjust_remaining <= 1 else 0.08
        targets_deg = {
            str(self._config.pan_joint): float(self._state.current_target_pan_deg)
            + self._rng.uniform(
                -float(self._config.pan_range_deg) * scale,
                float(self._config.pan_range_deg) * scale,
            ),
            str(self._config.tilt_joint): float(self._state.current_target_tilt_deg)
            + self._rng.uniform(
                -float(self._config.tilt_range_deg) * scale,
                float(self._config.tilt_range_deg) * scale,
            ),
        }

        if self._has_reframe_joint() and self._state.current_target_reframe_deg is not None:
            targets_deg[str(self._reframe_joint)] = float(self._state.current_target_reframe_deg) + self._rng.uniform(
                -float(self._config.reframe_range_deg) * scale * 0.55,
                float(self._config.reframe_range_deg) * scale * 0.55,
            )
        return targets_deg

    def start(
        self,
        *,
        anchor_pan_deg: float,
        anchor_tilt_deg: float,
        anchor_reframe_deg: float | None = None,
        now_monotonic: float | None = None,
    ) -> None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._state.enabled = True
        self._state.phase = "observe"
        self._state.phase_deadline_monotonic = now + self._sample_observe_duration()
        self._state.anchor_pan_deg = float(anchor_pan_deg)
        self._state.anchor_tilt_deg = float(anchor_tilt_deg)
        self._state.anchor_reframe_deg = (
            float(anchor_reframe_deg) if anchor_reframe_deg is not None else None
        )
        self._state.current_target_pan_deg = None
        self._state.current_target_tilt_deg = None
        self._state.current_target_reframe_deg = None
        self._state.shot_name = "center"
        self._state.micro_adjust_remaining = 0
        self._state.last_command_duration_sec = None

    def ensure_started(
        self,
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        current_reframe_deg: float | None = None,
        now_monotonic: float | None = None,
    ) -> None:
        if self._state.enabled:
            return
        self.start(
            anchor_pan_deg=float(current_pan_deg),
            anchor_tilt_deg=float(current_tilt_deg),
            anchor_reframe_deg=(
                float(current_reframe_deg) if current_reframe_deg is not None else None
            ),
            now_monotonic=now_monotonic,
        )

    def stop(self) -> None:
        self._state = IdleScanState()

    def tick(
        self,
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        current_reframe_deg: float | None = None,
        now_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        if not self._state.enabled:
            return None

        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if self._state.anchor_pan_deg is None:
            self._state.anchor_pan_deg = float(current_pan_deg)
        if self._state.anchor_tilt_deg is None:
            self._state.anchor_tilt_deg = float(current_tilt_deg)
        if self._state.anchor_reframe_deg is None and current_reframe_deg is not None:
            self._state.anchor_reframe_deg = float(current_reframe_deg)

        if self._state.phase in {"moving_reframe", "moving_micro"}:
            if now < float(self._state.phase_deadline_monotonic):
                return None
            self._state.phase = "settle"
            self._state.phase_deadline_monotonic = now + self._sample_settle_duration()
            return None

        if self._state.phase in {"observe", "linger"}:
            if now < float(self._state.phase_deadline_monotonic):
                return None
            _shot_name, targets_deg = self._compose_main_targets()
            duration = self._main_move_duration_for_targets(
                targets_deg,
                current_pan_deg=float(current_pan_deg),
                current_tilt_deg=float(current_tilt_deg),
                current_reframe_deg=(
                    float(current_reframe_deg) if current_reframe_deg is not None else None
                ),
            )
            self._state.phase = "moving_reframe"
            self._state.phase_deadline_monotonic = now + float(duration) + 0.05
            self._state.last_command_duration_sec = float(duration)
            return {
                "targets_deg": targets_deg,
                "duration_sec": float(duration),
            }

        if self._state.phase == "settle" and now < float(self._state.phase_deadline_monotonic):
            return None

        if self._state.phase == "settle":
            if self._state.micro_adjust_remaining > 0:
                targets_deg = self._compose_micro_adjust_targets()
                duration = self._micro_move_duration_for_targets(
                    targets_deg,
                    current_pan_deg=float(current_pan_deg),
                    current_tilt_deg=float(current_tilt_deg),
                    current_reframe_deg=(
                        float(current_reframe_deg) if current_reframe_deg is not None else None
                    ),
                )
                self._state.phase = "moving_micro"
                self._state.phase_deadline_monotonic = now + float(duration) + 0.05
                self._state.last_command_duration_sec = float(duration)
                self._state.micro_adjust_remaining = max(0, int(self._state.micro_adjust_remaining) - 1)
                self._state.current_target_pan_deg = float(targets_deg[str(self._config.pan_joint)])
                self._state.current_target_tilt_deg = float(targets_deg[str(self._config.tilt_joint)])
                if self._has_reframe_joint() and str(self._reframe_joint) in targets_deg:
                    self._state.current_target_reframe_deg = float(
                        targets_deg[str(self._reframe_joint)]
                    )
                return {
                    "targets_deg": targets_deg,
                    "duration_sec": float(duration),
                }

            self._state.phase = "linger"
            self._state.phase_deadline_monotonic = now + self._sample_linger_duration()
            return None

        self._state.phase = "observe"
        self._state.phase_deadline_monotonic = now + self._sample_observe_duration()
        return None

    def status_payload(
        self,
        *,
        running: bool,
        started_at: float | None,
        last_error: str,
    ) -> dict[str, Any]:
        payload = build_default_idle_scan_payload(self._config)
        coarse_phase = "none"
        if self._state.phase.startswith("moving"):
            coarse_phase = "moving"
        elif self._state.phase != "none":
            coarse_phase = "dwell"
        payload.update(
            {
                "enabled": bool(self._state.enabled and running),
                "running": bool(running),
                "phase": coarse_phase,
                "scan_phase": str(self._state.phase or "none"),
                "anchor_pan_deg": self._state.anchor_pan_deg,
                "anchor_tilt_deg": self._state.anchor_tilt_deg,
                "anchor_reframe_deg": self._state.anchor_reframe_deg,
                "current_target_pan_deg": self._state.current_target_pan_deg,
                "current_target_tilt_deg": self._state.current_target_tilt_deg,
                "current_target_reframe_deg": self._state.current_target_reframe_deg,
                "shot_name": str(self._state.shot_name or "none"),
                "micro_adjust_remaining": int(self._state.micro_adjust_remaining),
                "last_command_duration_sec": self._state.last_command_duration_sec,
                "started_at": started_at,
                "last_error": str(last_error or "").strip(),
            }
        )
        if coarse_phase == "dwell" and self._state.phase_deadline_monotonic > 0.0:
            payload["dwell_remaining_sec"] = max(
                0.0,
                float(self._state.phase_deadline_monotonic) - time.monotonic(),
            )
        return payload


class IdleScanWorker:
    def __init__(
        self,
        *,
        robot: Any,
        robot_lock: threading.RLock,
        config: IdleScanConfig,
    ) -> None:
        self._robot = robot
        self._robot_lock = robot_lock
        self._config = config
        self._planner = IdleScanPlanner(config)
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._started_at = time.time()
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="QuickControlIdleScan",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._planner.stop()

    def is_running(self) -> bool:
        return self._thread.is_alive() and not self._stop_event.is_set()

    def status_payload(self) -> dict[str, Any]:
        with self._status_lock:
            return self._planner.status_payload(
                running=self.is_running(),
                started_at=self._started_at,
                last_error=self._last_error,
            )

    def _sleep(self) -> None:
        self._stop_event.wait(max(0.02, float(self._config.poll_interval_sec)))

    def _clamp_targets_locked(self, targets_deg: dict[str, float]) -> dict[str, float]:
        return clamp_targets_deg(self._robot, targets_deg)

    def _interpolation_update_hz(self) -> float:
        controller = getattr(self._robot, "_controller", None)
        controller_config = getattr(controller, "config", None)
        joint_update_hz = getattr(controller_config, "joint_update_hz", None)
        try:
            return _clamp(
                max(float(joint_update_hz), float(_DEFAULT_IDLE_SCAN_INTERPOLATION_HZ)),
                4.0,
                60.0,
            )
        except Exception:
            pass

        robot_config = getattr(self._robot, "config", None)
        if isinstance(robot_config, dict):
            control_config = robot_config.get("control")
            if isinstance(control_config, dict):
                try:
                    return _clamp(
                        max(
                            float(control_config.get("hz", _DEFAULT_IDLE_SCAN_INTERPOLATION_HZ)),
                            float(_DEFAULT_IDLE_SCAN_INTERPOLATION_HZ),
                        ),
                        4.0,
                        60.0,
                    )
                except Exception:
                    pass
            transport_config = robot_config.get("transport")
            if isinstance(transport_config, dict):
                try:
                    return _clamp(
                        max(
                            float(transport_config.get("update_hz", _DEFAULT_IDLE_SCAN_INTERPOLATION_HZ)),
                            float(_DEFAULT_IDLE_SCAN_INTERPOLATION_HZ),
                        ),
                        4.0,
                        60.0,
                    )
                except Exception:
                    pass
        return float(_DEFAULT_IDLE_SCAN_INTERPOLATION_HZ)

    def _command_start_targets(
        self,
        *,
        joint_state: Any,
        targets_deg: dict[str, float],
    ) -> dict[str, float]:
        start_targets_deg: dict[str, float] = {}
        for joint_name, target_deg in targets_deg.items():
            try:
                start_targets_deg[str(joint_name)] = float(joint_state[str(joint_name)])
            except Exception:
                start_targets_deg[str(joint_name)] = float(target_deg)
        return start_targets_deg

    def _execute_interpolated_command(
        self,
        *,
        start_targets_deg: dict[str, float],
        targets_deg: dict[str, float],
        duration_sec: float,
    ) -> None:
        if not targets_deg:
            return

        duration = max(0.0, float(duration_sec))
        if duration <= 0.0:
            with self._robot_lock:
                if self._stop_event.is_set():
                    return
                self._robot.move_joints(targets_deg, duration=0.0, wait=False)
            return

        controller = getattr(self._robot, "_controller", None)
        can_use_sdk_raw_path = all(
            hasattr(controller, attr_name)
            for attr_name in (
                "_ensure_bus",
                "_read_raw_present_position",
                "_build_state",
                "_coerce_joint_targets_deg",
                "_joint_deg_to_relative_raw",
                "_relative_raw_to_absolute_goal_raw",
                "_write_raw_goal_positions",
            )
        )
        pan_joint_name = str(self._config.pan_joint)
        pan_total_delta_deg = float(
            float(targets_deg.get(pan_joint_name, start_targets_deg.get(pan_joint_name, 0.0)))
            - float(start_targets_deg.get(pan_joint_name, targets_deg.get(pan_joint_name, 0.0)))
        )
        apply_reverse_pan_compensation = _should_apply_reverse_pan_compensation(pan_total_delta_deg)
        if can_use_sdk_raw_path:
            with self._robot_lock:
                if self._stop_event.is_set():
                    return
                bus = controller._ensure_bus()
                raw_present_before = controller._read_raw_present_position(bus)
                before_state = controller._build_state(raw_present_before)
                target_deg_by_joint = controller._coerce_joint_targets_deg(targets_deg)

            update_hz = self._interpolation_update_hz()
            step_count = int(round(duration * update_hz))
            if step_count <= 1:
                with self._robot_lock:
                    if self._stop_event.is_set():
                        return
                    self._robot.move_joints(targets_deg, duration=duration, wait=False)
                return

            start_relative_raw_by_joint = {
                str(joint_name): float(before_state["relative_raw_position"][joint_name])
                for joint_name in target_deg_by_joint
            }
            target_relative_raw_by_joint = {
                str(joint_name): float(
                    controller._joint_deg_to_relative_raw(joint_name, target_deg_by_joint[joint_name])
                )
                for joint_name in target_deg_by_joint
            }
            prev_relative_raw_by_joint = dict(start_relative_raw_by_joint)
            pan_raw_direction = 0.0
            if apply_reverse_pan_compensation and pan_joint_name in target_relative_raw_by_joint:
                raw_delta = (
                    float(target_relative_raw_by_joint[pan_joint_name])
                    - float(start_relative_raw_by_joint[pan_joint_name])
                )
                pan_raw_direction = 1.0 if raw_delta >= 0.0 else -1.0

            step_duration = duration / float(step_count)
            started_at = time.monotonic()
            for step_index in range(1, step_count + 1):
                if self._stop_event.is_set():
                    return

                progress = float(step_index) / float(step_count)
                alpha = _smoothstep(progress)
                step_goal_raw: dict[str, int] = {}
                step_relative_raw_by_joint: dict[str, float] = {}
                for joint_name, target_relative_raw in target_relative_raw_by_joint.items():
                    start_relative_raw = float(start_relative_raw_by_joint[joint_name])
                    joint_alpha = (
                        _reverse_pan_launch_alpha(progress)
                        if apply_reverse_pan_compensation and str(joint_name) == pan_joint_name
                        else alpha
                    )
                    interpolated_relative_raw = start_relative_raw + (
                        float(target_relative_raw) - start_relative_raw
                    ) * joint_alpha
                    if (
                        apply_reverse_pan_compensation
                        and str(joint_name) == pan_joint_name
                    ):
                        previous_relative_raw = float(prev_relative_raw_by_joint[joint_name])
                        if pan_raw_direction > 0.0:
                            interpolated_relative_raw = max(float(interpolated_relative_raw), previous_relative_raw)
                        elif pan_raw_direction < 0.0:
                            interpolated_relative_raw = min(float(interpolated_relative_raw), previous_relative_raw)
                    step_relative_raw_by_joint[str(joint_name)] = float(interpolated_relative_raw)
                    step_goal_raw[str(joint_name)] = int(
                        controller._relative_raw_to_absolute_goal_raw(
                            joint_name,
                            interpolated_relative_raw,
                        )
                    )

                with self._robot_lock:
                    if self._stop_event.is_set():
                        return
                    controller._write_raw_goal_positions(bus, step_goal_raw)
                    last_multi_turn_goal_raw_mod = getattr(controller, "_last_multi_turn_goal_raw_mod", None)
                    if isinstance(last_multi_turn_goal_raw_mod, dict):
                        for joint_name, goal_raw in step_goal_raw.items():
                            if joint_name in last_multi_turn_goal_raw_mod:
                                last_multi_turn_goal_raw_mod[str(joint_name)] = int(goal_raw)
                prev_relative_raw_by_joint.update(step_relative_raw_by_joint)

                if step_index >= step_count:
                    break

                next_step_deadline = started_at + float(step_duration) * float(step_index)
                wait_remaining = next_step_deadline - time.monotonic()
                if wait_remaining > 0.0 and self._stop_event.wait(wait_remaining):
                    return
            return

        update_hz = self._interpolation_update_hz()
        step_count = int(round(duration * update_hz))
        if step_count <= 1:
            with self._robot_lock:
                if self._stop_event.is_set():
                    return
                self._robot.move_joints(targets_deg, duration=duration, wait=False)
            return

        step_duration = duration / float(step_count)
        started_at = time.monotonic()
        prev_step_targets_deg = dict(start_targets_deg)

        for step_index in range(1, step_count + 1):
            if self._stop_event.is_set():
                return

            progress = float(step_index) / float(step_count)
            alpha = _smoothstep(progress)
            step_targets_deg: dict[str, float] = {}
            for joint_name, target_deg in targets_deg.items():
                start_deg = float(start_targets_deg.get(joint_name, target_deg))
                joint_alpha = (
                    _reverse_pan_launch_alpha(progress)
                    if apply_reverse_pan_compensation and str(joint_name) == pan_joint_name
                    else alpha
                )
                interpolated_target_deg = start_deg + (
                    float(target_deg) - start_deg
                ) * joint_alpha
                if (
                    apply_reverse_pan_compensation
                    and str(joint_name) == pan_joint_name
                ):
                    previous_target_deg = float(prev_step_targets_deg.get(joint_name, start_deg))
                    interpolated_target_deg = min(float(interpolated_target_deg), previous_target_deg)
                step_targets_deg[str(joint_name)] = float(interpolated_target_deg)

            with self._robot_lock:
                if self._stop_event.is_set():
                    return
                clamped_targets_deg = self._clamp_targets_locked(step_targets_deg)
                self._robot.move_joints(
                    clamped_targets_deg,
                    duration=float(step_duration),
                    wait=False,
                )
            prev_step_targets_deg = dict(step_targets_deg)

            if step_index >= step_count:
                break

            next_step_deadline = started_at + float(step_duration) * float(step_index)
            wait_remaining = next_step_deadline - time.monotonic()
            if wait_remaining > 0.0 and self._stop_event.wait(wait_remaining):
                return

    def _run(self) -> None:
        while not self._stop_event.is_set():
            executed_motion = False
            try:
                pending_command: dict[str, Any] | None = None
                with self._robot_lock:
                    if self._stop_event.is_set():
                        break
                    current_state = self._robot.get_state()
                    joint_state = current_state["joint_state"]
                    current_pan_deg = float(joint_state[str(self._config.pan_joint)])
                    current_tilt_deg = float(joint_state[str(self._config.tilt_joint)])
                    current_reframe_deg = None
                    reframe_joint_name = _normalize_optional_joint_name(self._config.reframe_joint)
                    if reframe_joint_name is not None:
                        current_reframe_deg = float(joint_state[reframe_joint_name])
                    self._planner.ensure_started(
                        current_pan_deg=current_pan_deg,
                        current_tilt_deg=current_tilt_deg,
                        current_reframe_deg=current_reframe_deg,
                    )
                    command = self._planner.tick(
                        current_pan_deg=current_pan_deg,
                        current_tilt_deg=current_tilt_deg,
                        current_reframe_deg=current_reframe_deg,
                    )
                    if command is not None and dict(command.get("targets_deg") or {}):
                        targets_deg = self._clamp_targets_locked(
                            dict(command["targets_deg"])
                        )
                        pending_command = {
                            "start_targets_deg": self._command_start_targets(
                                joint_state=joint_state,
                                targets_deg=targets_deg,
                            ),
                            "targets_deg": targets_deg,
                            "duration_sec": float(command["duration_sec"]),
                        }
                if pending_command is not None:
                    self._execute_interpolated_command(
                        start_targets_deg=dict(pending_command["start_targets_deg"]),
                        targets_deg=dict(pending_command["targets_deg"]),
                        duration_sec=float(pending_command["duration_sec"]),
                    )
                    executed_motion = True
                with self._status_lock:
                    self._last_error = ""
            except Exception as exc:  # noqa: BLE001
                with self._status_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
            if not executed_motion:
                self._sleep()

        self._planner.stop()


__all__ = [
    "DEFAULT_IDLE_SCAN_DWELL_SEC_MAX",
    "DEFAULT_IDLE_SCAN_DWELL_SEC_MIN",
    "DEFAULT_IDLE_SCAN_MOVE_DURATION_MAX_SEC",
    "DEFAULT_IDLE_SCAN_MOVE_DURATION_MIN_SEC",
    "DEFAULT_IDLE_SCAN_PAN_JOINT",
    "DEFAULT_IDLE_SCAN_PAN_RANGE_DEG",
    "DEFAULT_IDLE_SCAN_POLL_INTERVAL_SEC",
    "DEFAULT_IDLE_SCAN_REFRAME_JOINT",
    "DEFAULT_IDLE_SCAN_REFRAME_RANGE_DEG",
    "DEFAULT_IDLE_SCAN_SPEED_PERCENT",
    "DEFAULT_IDLE_SCAN_TILT_JOINT",
    "DEFAULT_IDLE_SCAN_TILT_RANGE_DEG",
    "IdleScanConfig",
    "IdleScanPlanner",
    "IdleScanState",
    "IdleScanWorker",
    "build_default_idle_scan_payload",
    "clamp_targets_deg",
]
