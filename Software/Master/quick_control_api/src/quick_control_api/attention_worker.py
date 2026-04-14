from __future__ import annotations

import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .face_follow_worker import (
    AxisFollowState,
    DEFAULT_COMMAND_MODE,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_LATEST_URL,
    DEFAULT_MAX_PAN_STEP_DEG,
    DEFAULT_MAX_TILT_STEP_DEG,
    DEFAULT_MIN_PAN_STEP_DEG,
    DEFAULT_MIN_TILT_STEP_DEG,
    DEFAULT_MOVE_DURATION_S,
    DEFAULT_PAN_BREAKAWAY_STEP_DEG,
    DEFAULT_PAN_BREAKAWAY_STEP_POS_DEG,
    DEFAULT_PAN_BREAKAWAY_STEP_NEG_DEG,
    DEFAULT_PAN_DEAD_ZONE_NORM,
    DEFAULT_PAN_GAIN_DEG_PER_NORM,
    DEFAULT_PAN_JOINT,
    DEFAULT_PAN_MIN_STEP_ZONE_NORM,
    DEFAULT_PAN_NEGATIVE_SCALE,
    DEFAULT_PAN_RESUME_ZONE_NORM,
    DEFAULT_PAN_SIGN,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_STICTION_EPS_DEG,
    DEFAULT_STICTION_FRAMES,
    DEFAULT_TARGET_KIND,
    DEFAULT_TILT_BREAKAWAY_STEP_DEG,
    DEFAULT_TILT_DEAD_ZONE_NORM,
    DEFAULT_TILT_GAIN_DEG_PER_NORM,
    DEFAULT_TILT_JOINT,
    DEFAULT_TILT_MIN_STEP_ZONE_NORM,
    DEFAULT_TILT_RESUME_ZONE_NORM,
    DEFAULT_TILT_SIGN,
    DEFAULT_LIMIT_MARGIN_RAW,
    _apply_stiction_breakaway,
    _compute_joint_step,
    _extract_target_center_norm,
    _fetch_latest,
    _reset_axis_state,
    _single_turn_limit_warning,
)
from .idle_scan_worker import (
    DEFAULT_IDLE_SCAN_DWELL_SEC_MAX,
    DEFAULT_IDLE_SCAN_DWELL_SEC_MIN,
    DEFAULT_IDLE_SCAN_MOVE_DURATION_MAX_SEC,
    DEFAULT_IDLE_SCAN_MOVE_DURATION_MIN_SEC,
    DEFAULT_IDLE_SCAN_PAN_RANGE_DEG,
    DEFAULT_IDLE_SCAN_SPEED_PERCENT,
    DEFAULT_IDLE_SCAN_TILT_RANGE_DEG,
    IdleScanConfig,
    IdleScanPlanner,
    build_default_idle_scan_payload,
    clamp_targets_deg,
)


def _normalize_optional_joint_name(joint_name: str | None) -> str | None:
    normalized = str(joint_name or "").strip()
    return normalized or None

DEFAULT_LOST_TARGET_HOLD_SEC = 1.0


@dataclass
class AttentionConfig:
    target_kind: str = DEFAULT_TARGET_KIND
    latest_url: str = DEFAULT_LATEST_URL
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    http_timeout: float = DEFAULT_HTTP_TIMEOUT_S
    move_duration: float = DEFAULT_MOVE_DURATION_S
    pan_joint: str = DEFAULT_PAN_JOINT
    tilt_joint: str = DEFAULT_TILT_JOINT
    pan_sign: float = DEFAULT_PAN_SIGN
    tilt_sign: float = DEFAULT_TILT_SIGN
    pan_gain: float = DEFAULT_PAN_GAIN_DEG_PER_NORM
    tilt_gain: float = DEFAULT_TILT_GAIN_DEG_PER_NORM
    pan_dead_zone: float = DEFAULT_PAN_DEAD_ZONE_NORM
    tilt_dead_zone: float = DEFAULT_TILT_DEAD_ZONE_NORM
    pan_resume_zone: float = DEFAULT_PAN_RESUME_ZONE_NORM
    tilt_resume_zone: float = DEFAULT_TILT_RESUME_ZONE_NORM
    min_pan_step: float = DEFAULT_MIN_PAN_STEP_DEG
    min_tilt_step: float = DEFAULT_MIN_TILT_STEP_DEG
    pan_min_step_zone: float = DEFAULT_PAN_MIN_STEP_ZONE_NORM
    tilt_min_step_zone: float = DEFAULT_TILT_MIN_STEP_ZONE_NORM
    max_pan_step: float = DEFAULT_MAX_PAN_STEP_DEG
    max_tilt_step: float = DEFAULT_MAX_TILT_STEP_DEG
    command_mode: str = DEFAULT_COMMAND_MODE
    limit_margin_raw: int = DEFAULT_LIMIT_MARGIN_RAW
    stiction_eps_deg: float = DEFAULT_STICTION_EPS_DEG
    stiction_frames: int = DEFAULT_STICTION_FRAMES
    pan_breakaway_step: float = DEFAULT_PAN_BREAKAWAY_STEP_DEG
    pan_breakaway_step_pos: float | None = DEFAULT_PAN_BREAKAWAY_STEP_POS_DEG
    pan_breakaway_step_neg: float = DEFAULT_PAN_BREAKAWAY_STEP_NEG_DEG
    pan_negative_scale: float = DEFAULT_PAN_NEGATIVE_SCALE
    tilt_breakaway_step: float = DEFAULT_TILT_BREAKAWAY_STEP_DEG
    enable_idle_scan_fallback: bool = True
    lost_target_hold_sec: float = DEFAULT_LOST_TARGET_HOLD_SEC
    idle_scan: IdleScanConfig = field(default_factory=IdleScanConfig)


def build_default_attention_payload(config: AttentionConfig | None = None) -> dict[str, Any]:
    cfg = config or AttentionConfig()
    payload = {
        "enabled": False,
        "running": False,
        "behavior_mode": "attention",
        "mode": "hold",
        "target_kind": str(cfg.target_kind),
        "latest_url": str(cfg.latest_url),
        "poll_interval_sec": float(cfg.poll_interval),
        "http_timeout_sec": float(cfg.http_timeout),
        "move_duration_sec": float(cfg.move_duration),
        "command_mode": str(cfg.command_mode),
        "pan_joint": str(cfg.pan_joint),
        "tilt_joint": str(cfg.tilt_joint),
        "pan_sign": float(cfg.pan_sign),
        "tilt_sign": float(cfg.tilt_sign),
        "target_visible": False,
        "last_frame_id": 0,
        "last_result_status": "",
        "last_error": "",
        "last_hold_reason": "",
        "last_limit_warning": "",
        "last_observation_age_ms": None,
        "last_seen_age_ms": None,
        "last_target_center_norm": [0.50, 0.42],
        "last_face_center": None,
        "last_offset_norm": {"ndx": 0.0, "ndy": 0.0},
        "last_joint_step_deg": {},
        "idle_fallback_enabled": bool(cfg.enable_idle_scan_fallback),
        "lost_target_hold_sec": float(cfg.lost_target_hold_sec),
        "idle_scan": build_default_idle_scan_payload(cfg.idle_scan),
        "started_at": None,
        "config": asdict(cfg),
    }
    return payload


class AttentionWorker:
    def __init__(
        self,
        *,
        robot: Any,
        robot_lock: threading.RLock,
        config: AttentionConfig,
    ) -> None:
        self._robot = robot
        self._robot_lock = robot_lock
        self._config = config
        self._idle_scan = IdleScanPlanner(config.idle_scan, rng=random.Random())
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status = build_default_attention_payload(config)
        self._status["enabled"] = True
        self._status["running"] = False
        self._status["started_at"] = time.time()
        self._last_observation_monotonic = 0.0
        self._last_seen_monotonic = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="QuickControlAttention",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._idle_scan.stop()
        with self._status_lock:
            self._status["enabled"] = False
            self._status["running"] = False

    def is_running(self) -> bool:
        return self._thread.is_alive() and not self._stop_event.is_set()

    def status_payload(self) -> dict[str, Any]:
        with self._status_lock:
            payload = dict(self._status)
            payload["running"] = self.is_running()
            payload["enabled"] = bool(payload["running"])
            if self._last_observation_monotonic > 0.0:
                payload["last_observation_age_ms"] = max(
                    0.0,
                    (time.monotonic() - float(self._last_observation_monotonic)) * 1000.0,
                )
            else:
                payload["last_observation_age_ms"] = None
            payload["last_seen_age_ms"] = max(
                0.0,
                (time.monotonic() - float(self._last_seen_monotonic)) * 1000.0,
            )
            return payload

    def _sleep(self) -> None:
        self._stop_event.wait(max(0.01, float(self._config.poll_interval)))

    def _update_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _update_idle_scan_status(
        self,
        *,
        running: bool,
        last_error: str = "",
    ) -> None:
        self._update_status(
            idle_scan=self._idle_scan.status_payload(
                running=running,
                started_at=float(self._status.get("started_at") or time.time()),
                last_error=last_error,
            )
        )

    def _hold_or_scan_without_target(self, *, result_status: str) -> None:
        now = time.monotonic()
        lost_target_age_sec = max(0.0, now - float(self._last_seen_monotonic))
        hold_sec = max(0.0, float(self._config.lost_target_hold_sec))
        if (
            not bool(self._config.enable_idle_scan_fallback)
            or lost_target_age_sec < hold_sec
        ):
            self._idle_scan.stop()
            remaining_hold_sec = max(0.0, hold_sec - lost_target_age_sec)
            hold_reason = (
                f"waiting {remaining_hold_sec:.2f}s before idle scan"
                if bool(self._config.enable_idle_scan_fallback) and remaining_hold_sec > 0.0
                else f"status={result_status}"
            )
            self._update_idle_scan_status(running=self.is_running(), last_error="")
            self._update_status(
                mode="hold",
                target_visible=False,
                last_hold_reason=hold_reason,
                last_joint_step_deg={},
                last_limit_warning="",
            )
            self._sleep()
            return

        try:
            with self._robot_lock:
                if self._stop_event.is_set():
                    return
                current_state = self._robot.get_state()
                joint_state = current_state["joint_state"]
                current_pan_deg = float(joint_state[str(self._config.idle_scan.pan_joint)])
                current_tilt_deg = float(joint_state[str(self._config.idle_scan.tilt_joint)])
                current_reframe_deg = None
                reframe_joint_name = _normalize_optional_joint_name(
                    self._config.idle_scan.reframe_joint
                )
                if reframe_joint_name is not None:
                    current_reframe_deg = float(joint_state[reframe_joint_name])
                self._idle_scan.ensure_started(
                    current_pan_deg=current_pan_deg,
                    current_tilt_deg=current_tilt_deg,
                    current_reframe_deg=current_reframe_deg,
                    now_monotonic=now,
                )
                command = self._idle_scan.tick(
                    current_pan_deg=current_pan_deg,
                    current_tilt_deg=current_tilt_deg,
                    current_reframe_deg=current_reframe_deg,
                    now_monotonic=now,
                )
                if command is not None and dict(command.get("targets_deg") or {}):
                    targets_deg = clamp_targets_deg(
                        self._robot,
                        dict(command["targets_deg"]),
                    )
                    self._robot.move_joints(
                        targets_deg,
                        duration=float(command["duration_sec"]),
                        wait=False,
                    )
            self._update_idle_scan_status(running=self.is_running(), last_error="")
            self._update_status(
                mode="scanning",
                target_visible=False,
                last_error="",
                last_hold_reason="idle scanning while target is missing",
                last_joint_step_deg={},
                last_limit_warning="",
            )
        except Exception as exc:  # noqa: BLE001
            self._idle_scan.stop()
            self._update_idle_scan_status(
                running=self.is_running(),
                last_error=f"{type(exc).__name__}: {exc}",
            )
            self._update_status(
                mode="hold",
                target_visible=False,
                last_error=f"{type(exc).__name__}: {exc}",
                last_hold_reason=f"idle scan command failed: {type(exc).__name__}: {exc}",
                last_joint_step_deg={},
            )
        self._sleep()

    def _run(self) -> None:
        pan_axis_state = AxisFollowState()
        tilt_axis_state = AxisFollowState()
        last_frame_id = -1
        self._update_status(running=True, enabled=True, mode="hold")
        self._update_idle_scan_status(running=True, last_error="")

        pan_breakaway_step_pos = (
            float(self._config.pan_breakaway_step_pos)
            if self._config.pan_breakaway_step_pos is not None
            else float(self._config.pan_breakaway_step)
        )
        pan_breakaway_step_neg = (
            float(self._config.pan_breakaway_step_neg)
            if self._config.pan_breakaway_step_neg is not None
            else float(self._config.pan_breakaway_step)
        )

        while not self._stop_event.is_set():
            try:
                result = _fetch_latest(
                    str(self._config.latest_url),
                    float(self._config.http_timeout),
                )
            except Exception as exc:  # noqa: BLE001
                self._idle_scan.stop()
                self._update_idle_scan_status(
                    running=self.is_running(),
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                self._update_status(
                    mode="hold",
                    target_visible=False,
                    last_error=str(exc),
                    last_hold_reason=f"failed to fetch tracking result: {exc}",
                )
                self._sleep()
                continue

            if self._stop_event.is_set():
                break

            frame_id = int(result.get("frame_id", 0) or 0)
            if frame_id <= 0 or frame_id == last_frame_id:
                self._sleep()
                continue
            last_frame_id = frame_id

            result_status = str(result.get("status", "unknown"))
            self._update_status(
                last_frame_id=int(frame_id),
                last_result_status=result_status,
                last_error="",
            )

            if not bool(result.get("detected", False)):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._hold_or_scan_without_target(result_status=result_status)
                continue

            self._idle_scan.stop()
            self._last_seen_monotonic = time.monotonic()
            self._update_idle_scan_status(running=self.is_running(), last_error="")

            offset_payload = result.get("smoothed_offset") or result.get("offset")
            if not isinstance(offset_payload, dict):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
                    mode="hold",
                    target_visible=False,
                    last_hold_reason="tracking result missing offset payload",
                    last_joint_step_deg={},
                    last_limit_warning="",
                )
                self._sleep()
                continue

            try:
                ndx = float(offset_payload["ndx"])
                ndy = float(offset_payload["ndy"])
            except Exception:
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
                    mode="hold",
                    target_visible=False,
                    last_hold_reason="tracking result offset payload is invalid",
                    last_joint_step_deg={},
                    last_limit_warning="",
                )
                self._sleep()
                continue

            pan_step_deg = _compute_joint_step(
                axis_state=pan_axis_state,
                normalized_offset=ndx,
                gain_deg_per_norm=float(self._config.pan_gain),
                dead_zone_norm=float(self._config.pan_dead_zone),
                resume_zone_norm=float(self._config.pan_resume_zone),
                min_step_deg=float(self._config.min_pan_step),
                min_step_zone_norm=float(self._config.pan_min_step_zone),
                max_step_deg=float(self._config.max_pan_step),
                sign=float(self._config.pan_sign),
            )
            if pan_step_deg < 0.0:
                negative_scale = max(1.0, float(self._config.pan_negative_scale))
                pan_step_deg = max(
                    -abs(float(self._config.max_pan_step)) * negative_scale,
                    pan_step_deg * negative_scale,
                )
            tilt_step_deg = _compute_joint_step(
                axis_state=tilt_axis_state,
                normalized_offset=ndy,
                gain_deg_per_norm=float(self._config.tilt_gain),
                dead_zone_norm=float(self._config.tilt_dead_zone),
                resume_zone_norm=float(self._config.tilt_resume_zone),
                min_step_deg=float(self._config.min_tilt_step),
                min_step_zone_norm=float(self._config.tilt_min_step_zone),
                max_step_deg=float(self._config.max_tilt_step),
                sign=float(self._config.tilt_sign),
            )

            joint_targets: dict[str, float] = {}
            if abs(pan_step_deg) > 1e-9:
                joint_targets[str(self._config.pan_joint)] = float(pan_step_deg)
            if abs(tilt_step_deg) > 1e-9:
                tilt_joint_name = str(self._config.tilt_joint)
                joint_targets[tilt_joint_name] = (
                    float(joint_targets.get(tilt_joint_name, 0.0)) + float(tilt_step_deg)
                )

            target_x_norm, target_y_norm = _extract_target_center_norm(result)
            target_face = result.get("target_face")
            face_center = target_face.get("center") if isinstance(target_face, dict) else None
            self._last_observation_monotonic = time.monotonic()
            self._update_status(
                mode="tracking",
                target_visible=True,
                last_hold_reason="",
                last_target_center_norm=[float(target_x_norm), float(target_y_norm)],
                last_face_center=face_center,
                last_offset_norm={"ndx": float(ndx), "ndy": float(ndy)},
            )

            if not joint_targets:
                self._update_status(
                    last_hold_reason="inside dead zone",
                    last_joint_step_deg={},
                    last_limit_warning="",
                )
                self._sleep()
                continue

            try:
                with self._robot_lock:
                    if self._stop_event.is_set():
                        break
                    current_state = self._robot.get_state()
                    pan_joint_name = str(self._config.pan_joint)
                    tilt_joint_name = str(self._config.tilt_joint)

                    if pan_joint_name in joint_targets:
                        pan_step_deg, _ = _apply_stiction_breakaway(
                            pan_axis_state,
                            current_joint_deg=float(current_state["joint_state"][pan_joint_name]),
                            step_deg=float(joint_targets[pan_joint_name]),
                            movement_eps_deg=float(self._config.stiction_eps_deg),
                            stagnant_frame_threshold=int(self._config.stiction_frames),
                            breakaway_step_pos_deg=pan_breakaway_step_pos,
                            breakaway_step_neg_deg=pan_breakaway_step_neg,
                        )
                        joint_targets[pan_joint_name] = float(pan_step_deg)
                    else:
                        _apply_stiction_breakaway(
                            pan_axis_state,
                            current_joint_deg=float(current_state["joint_state"][pan_joint_name]),
                            step_deg=0.0,
                            movement_eps_deg=float(self._config.stiction_eps_deg),
                            stagnant_frame_threshold=int(self._config.stiction_frames),
                            breakaway_step_pos_deg=pan_breakaway_step_pos,
                            breakaway_step_neg_deg=pan_breakaway_step_neg,
                        )

                    if tilt_joint_name in joint_targets:
                        tilt_step_deg, _ = _apply_stiction_breakaway(
                            tilt_axis_state,
                            current_joint_deg=float(current_state["joint_state"][tilt_joint_name]),
                            step_deg=float(joint_targets[tilt_joint_name]),
                            movement_eps_deg=float(self._config.stiction_eps_deg),
                            stagnant_frame_threshold=int(self._config.stiction_frames),
                            breakaway_step_pos_deg=float(self._config.tilt_breakaway_step),
                            breakaway_step_neg_deg=float(self._config.tilt_breakaway_step),
                        )
                        joint_targets[tilt_joint_name] = float(tilt_step_deg)
                    else:
                        _apply_stiction_breakaway(
                            tilt_axis_state,
                            current_joint_deg=float(current_state["joint_state"][tilt_joint_name]),
                            step_deg=0.0,
                            movement_eps_deg=float(self._config.stiction_eps_deg),
                            stagnant_frame_threshold=int(self._config.stiction_frames),
                            breakaway_step_pos_deg=float(self._config.tilt_breakaway_step),
                            breakaway_step_neg_deg=float(self._config.tilt_breakaway_step),
                        )

                    joint_targets = {
                        joint_name: float(delta_deg)
                        for joint_name, delta_deg in joint_targets.items()
                        if abs(float(delta_deg)) > 1e-9
                    }
                    if not joint_targets:
                        self._update_status(
                            last_hold_reason="inside dead zone",
                            last_joint_step_deg={},
                            last_limit_warning="",
                        )
                        self._sleep()
                        continue

                    limit_warning = None
                    for joint_name, delta_deg in joint_targets.items():
                        limit_warning = _single_turn_limit_warning(
                            self._robot,
                            current_state,
                            joint_name=joint_name,
                            delta_deg=float(delta_deg),
                            limit_margin_raw=int(self._config.limit_margin_raw),
                        )
                        if limit_warning is not None:
                            break

                    absolute_targets = {
                        joint_name: float(current_state["joint_state"][joint_name]) + float(delta_deg)
                        for joint_name, delta_deg in joint_targets.items()
                    }
                    self._robot.move_joints(
                        absolute_targets,
                        duration=float(self._config.move_duration),
                        wait=str(self._config.command_mode).strip().lower() != "stream",
                    )

                self._update_status(
                    last_error="",
                    last_hold_reason="",
                    last_limit_warning="" if limit_warning is None else str(limit_warning),
                    last_joint_step_deg={
                        joint_name: float(delta_deg)
                        for joint_name, delta_deg in joint_targets.items()
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
                    mode="hold",
                    target_visible=False,
                    last_error=f"{type(exc).__name__}: {exc}",
                    last_hold_reason=f"hardware command failed: {type(exc).__name__}: {exc}",
                    last_joint_step_deg={},
                )

            self._sleep()

        self._idle_scan.stop()
        self._update_idle_scan_status(running=False, last_error="")
        self._update_status(running=False, enabled=False)


__all__ = [
    "AttentionConfig",
    "AttentionWorker",
    "DEFAULT_LOST_TARGET_HOLD_SEC",
    "build_default_attention_payload",
]
