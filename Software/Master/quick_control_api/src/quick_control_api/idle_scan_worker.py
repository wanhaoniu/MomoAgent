from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_IDLE_SCAN_PAN_JOINT = "shoulder_pan"
DEFAULT_IDLE_SCAN_TILT_JOINT = "elbow_flex"
DEFAULT_IDLE_SCAN_SPEED_PERCENT = 25
DEFAULT_IDLE_SCAN_PAN_RANGE_DEG = 10.0
DEFAULT_IDLE_SCAN_TILT_RANGE_DEG = 8.0
DEFAULT_IDLE_SCAN_MOVE_DURATION_MIN_SEC = 1.2
DEFAULT_IDLE_SCAN_MOVE_DURATION_MAX_SEC = 2.8
DEFAULT_IDLE_SCAN_DWELL_SEC_MIN = 0.8
DEFAULT_IDLE_SCAN_DWELL_SEC_MAX = 2.5
DEFAULT_IDLE_SCAN_POLL_INTERVAL_SEC = 0.10


@dataclass
class IdleScanConfig:
    pan_joint: str = DEFAULT_IDLE_SCAN_PAN_JOINT
    tilt_joint: str = DEFAULT_IDLE_SCAN_TILT_JOINT
    speed_percent: int = DEFAULT_IDLE_SCAN_SPEED_PERCENT
    pan_range_deg: float = DEFAULT_IDLE_SCAN_PAN_RANGE_DEG
    tilt_range_deg: float = DEFAULT_IDLE_SCAN_TILT_RANGE_DEG
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
    current_target_pan_deg: float | None = None
    current_target_tilt_deg: float | None = None
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
        "speed_percent": int(cfg.speed_percent),
        "pan_range_deg": float(cfg.pan_range_deg),
        "tilt_range_deg": float(cfg.tilt_range_deg),
        "move_duration_min_sec": float(cfg.move_duration_min_sec),
        "move_duration_max_sec": float(cfg.move_duration_max_sec),
        "dwell_sec_min": float(cfg.dwell_sec_min),
        "dwell_sec_max": float(cfg.dwell_sec_max),
        "poll_interval_sec": float(cfg.poll_interval_sec),
        "anchor_pan_deg": None,
        "anchor_tilt_deg": None,
        "current_target_pan_deg": None,
        "current_target_tilt_deg": None,
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


class IdleScanPlanner:
    def __init__(self, config: IdleScanConfig, *, rng: random.Random | None = None) -> None:
        self._config = config
        self._state = IdleScanState()
        self._rng = rng or random.Random()

    @property
    def state(self) -> IdleScanState:
        return self._state

    def start(
        self,
        *,
        anchor_pan_deg: float,
        anchor_tilt_deg: float,
        now_monotonic: float | None = None,
    ) -> None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._state.enabled = True
        self._state.phase = "dwell"
        self._state.phase_deadline_monotonic = now
        self._state.anchor_pan_deg = float(anchor_pan_deg)
        self._state.anchor_tilt_deg = float(anchor_tilt_deg)
        self._state.current_target_pan_deg = None
        self._state.current_target_tilt_deg = None
        self._state.last_command_duration_sec = None

    def ensure_started(
        self,
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        now_monotonic: float | None = None,
    ) -> None:
        if self._state.enabled:
            return
        self.start(
            anchor_pan_deg=float(current_pan_deg),
            anchor_tilt_deg=float(current_tilt_deg),
            now_monotonic=now_monotonic,
        )

    def stop(self) -> None:
        self._state = IdleScanState()

    def tick(
        self,
        *,
        current_pan_deg: float,
        current_tilt_deg: float,
        now_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        if not self._state.enabled:
            return None

        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if self._state.anchor_pan_deg is None:
            self._state.anchor_pan_deg = float(current_pan_deg)
        if self._state.anchor_tilt_deg is None:
            self._state.anchor_tilt_deg = float(current_tilt_deg)

        if self._state.phase == "moving":
            if now < float(self._state.phase_deadline_monotonic):
                return None
            dwell_duration = self._rng.uniform(
                float(self._config.dwell_sec_min),
                float(self._config.dwell_sec_max),
            )
            self._state.phase = "dwell"
            self._state.phase_deadline_monotonic = now + float(dwell_duration)
            return None

        if self._state.phase == "dwell" and now < float(self._state.phase_deadline_monotonic):
            return None

        target_pan_deg = float(self._state.anchor_pan_deg) + self._rng.uniform(
            -float(self._config.pan_range_deg),
            float(self._config.pan_range_deg),
        )
        target_tilt_deg = float(self._state.anchor_tilt_deg) + self._rng.uniform(
            -float(self._config.tilt_range_deg),
            float(self._config.tilt_range_deg),
        )
        duration = self._rng.uniform(
            float(self._config.move_duration_min_sec),
            float(self._config.move_duration_max_sec),
        )
        self._state.phase = "moving"
        self._state.phase_deadline_monotonic = now + float(duration) + 0.05
        self._state.current_target_pan_deg = float(target_pan_deg)
        self._state.current_target_tilt_deg = float(target_tilt_deg)
        self._state.last_command_duration_sec = float(duration)
        return {
            "targets_deg": {
                str(self._config.pan_joint): float(target_pan_deg),
                str(self._config.tilt_joint): float(target_tilt_deg),
            },
            "duration_sec": float(duration),
        }

    def status_payload(
        self,
        *,
        running: bool,
        started_at: float | None,
        last_error: str,
    ) -> dict[str, Any]:
        payload = build_default_idle_scan_payload(self._config)
        payload.update(
            {
                "enabled": bool(self._state.enabled and running),
                "running": bool(running),
                "phase": str(self._state.phase or "none"),
                "anchor_pan_deg": self._state.anchor_pan_deg,
                "anchor_tilt_deg": self._state.anchor_tilt_deg,
                "current_target_pan_deg": self._state.current_target_pan_deg,
                "current_target_tilt_deg": self._state.current_target_tilt_deg,
                "last_command_duration_sec": self._state.last_command_duration_sec,
                "started_at": started_at,
                "last_error": str(last_error or "").strip(),
            }
        )
        if self._state.phase == "dwell" and self._state.phase_deadline_monotonic > 0.0:
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

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._robot_lock:
                    if self._stop_event.is_set():
                        break
                    current_state = self._robot.get_state()
                    joint_state = current_state["joint_state"]
                    current_pan_deg = float(joint_state[str(self._config.pan_joint)])
                    current_tilt_deg = float(joint_state[str(self._config.tilt_joint)])
                    self._planner.ensure_started(
                        current_pan_deg=current_pan_deg,
                        current_tilt_deg=current_tilt_deg,
                    )
                    command = self._planner.tick(
                        current_pan_deg=current_pan_deg,
                        current_tilt_deg=current_tilt_deg,
                    )
                    if command is not None:
                        targets_deg = self._clamp_targets_locked(
                            dict(command["targets_deg"])
                        )
                        self._robot.move_joints(
                            targets_deg,
                            duration=float(command["duration_sec"]),
                            wait=False,
                        )
                with self._status_lock:
                    self._last_error = ""
            except Exception as exc:  # noqa: BLE001
                with self._status_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
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
