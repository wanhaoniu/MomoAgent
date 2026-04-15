from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from .idle_scan_worker import clamp_targets_deg

DEFAULT_HAIGUITANG_PAN_JOINT = "shoulder_pan"
DEFAULT_HAIGUITANG_TILT_JOINT = "elbow_flex"
DEFAULT_HAIGUITANG_SPEED_PERCENT = 30
DEFAULT_HAIGUITANG_NOD_AMPLITUDE_DEG = 7.0
DEFAULT_HAIGUITANG_NOD_CYCLES = 2
DEFAULT_HAIGUITANG_SHAKE_AMPLITUDE_DEG = 10.0
DEFAULT_HAIGUITANG_SHAKE_CYCLES = 2
DEFAULT_HAIGUITANG_BEAT_DURATION_SEC = 0.26
DEFAULT_HAIGUITANG_BEAT_PAUSE_SEC = 0.08
DEFAULT_HAIGUITANG_RETURN_DURATION_SEC = 0.24
DEFAULT_HAIGUITANG_SETTLE_PAUSE_SEC = 0.10
DEFAULT_HAIGUITANG_POLL_INTERVAL_SEC = 0.05

ALLOWED_HAIGUITANG_ACTIONS = frozenset({"nod", "shake", "center", "reanchor"})
_STOP_SENTINEL = "__stop__"


@dataclass
class HaiGuiTangConfig:
    pan_joint: str = DEFAULT_HAIGUITANG_PAN_JOINT
    tilt_joint: str = DEFAULT_HAIGUITANG_TILT_JOINT
    speed_percent: int = DEFAULT_HAIGUITANG_SPEED_PERCENT
    nod_amplitude_deg: float = DEFAULT_HAIGUITANG_NOD_AMPLITUDE_DEG
    nod_cycles: int = DEFAULT_HAIGUITANG_NOD_CYCLES
    shake_amplitude_deg: float = DEFAULT_HAIGUITANG_SHAKE_AMPLITUDE_DEG
    shake_cycles: int = DEFAULT_HAIGUITANG_SHAKE_CYCLES
    beat_duration_sec: float = DEFAULT_HAIGUITANG_BEAT_DURATION_SEC
    beat_pause_sec: float = DEFAULT_HAIGUITANG_BEAT_PAUSE_SEC
    return_duration_sec: float = DEFAULT_HAIGUITANG_RETURN_DURATION_SEC
    settle_pause_sec: float = DEFAULT_HAIGUITANG_SETTLE_PAUSE_SEC
    poll_interval_sec: float = DEFAULT_HAIGUITANG_POLL_INTERVAL_SEC
    auto_center_after_action: bool = True
    capture_anchor_on_start: bool = True


def build_default_haiguitang_payload(
    config: HaiGuiTangConfig | None = None,
) -> dict[str, Any]:
    cfg = config or HaiGuiTangConfig()
    return {
        "enabled": False,
        "running": False,
        "behavior_mode": "haiguitang",
        "mode": "idle",
        "pan_joint": str(cfg.pan_joint),
        "tilt_joint": str(cfg.tilt_joint),
        "speed_percent": int(cfg.speed_percent),
        "nod_amplitude_deg": float(cfg.nod_amplitude_deg),
        "nod_cycles": int(cfg.nod_cycles),
        "shake_amplitude_deg": float(cfg.shake_amplitude_deg),
        "shake_cycles": int(cfg.shake_cycles),
        "beat_duration_sec": float(cfg.beat_duration_sec),
        "beat_pause_sec": float(cfg.beat_pause_sec),
        "return_duration_sec": float(cfg.return_duration_sec),
        "settle_pause_sec": float(cfg.settle_pause_sec),
        "poll_interval_sec": float(cfg.poll_interval_sec),
        "auto_center_after_action": bool(cfg.auto_center_after_action),
        "capture_anchor_on_start": bool(cfg.capture_anchor_on_start),
        "active_action": "none",
        "last_requested_action": "",
        "last_completed_action": "",
        "queue_length": 0,
        "completed_action_count": 0,
        "anchor_pan_deg": None,
        "anchor_tilt_deg": None,
        "last_targets_deg": {},
        "last_command_duration_sec": None,
        "last_error": "",
        "started_at": None,
        "config": asdict(cfg),
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


def _sleep_or_stop(stop_event: threading.Event, duration_sec: float) -> bool:
    return bool(stop_event.wait(max(0.0, float(duration_sec))))


def _normalize_action_name(action: str) -> str:
    normalized = str(action or "").strip().lower()
    if normalized not in ALLOWED_HAIGUITANG_ACTIONS:
        raise ValueError(f"Unsupported haiguitang action: {action}")
    return normalized


class HaiGuiTangWorker:
    def __init__(
        self,
        *,
        robot: Any,
        robot_lock: threading.RLock,
        config: HaiGuiTangConfig,
    ) -> None:
        self._robot = robot
        self._robot_lock = robot_lock
        self._config = config
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._status = build_default_haiguitang_payload(config)
        self._status["enabled"] = True
        self._status["running"] = False
        self._status["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._run,
            name="QuickControlHaiGuiTang",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._queue.put(_STOP_SENTINEL)
        with self._status_lock:
            self._status["enabled"] = False
            self._status["running"] = False
            self._status["queue_length"] = 0

    def is_running(self) -> bool:
        return self._thread.is_alive() and not self._stop_event.is_set()

    def enqueue_action(self, action: str) -> dict[str, Any]:
        normalized = _normalize_action_name(action)
        self._queue.put(normalized)
        with self._status_lock:
            self._status["last_requested_action"] = normalized
            self._status["queue_length"] = self._queue.qsize()
        return self.status_payload()

    def status_payload(self) -> dict[str, Any]:
        with self._status_lock:
            payload = dict(self._status)
            payload["running"] = self.is_running()
            payload["enabled"] = bool(payload["running"])
            payload["queue_length"] = max(0, int(self._queue.qsize()))
            return payload

    def _update_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)
            self._status["queue_length"] = max(0, int(self._queue.qsize()))

    def _scaled_duration(self, base_duration_sec: float) -> float:
        speed_ratio = max(0.10, float(self._config.speed_percent) / 100.0)
        return _clamp(float(base_duration_sec) / speed_ratio, 0.05, 3.0)

    def _capture_anchor_locked(self) -> tuple[float, float]:
        current_state = self._robot.get_state()
        joint_state = current_state["joint_state"]
        anchor_pan_deg = float(joint_state[str(self._config.pan_joint)])
        anchor_tilt_deg = float(joint_state[str(self._config.tilt_joint)])
        return anchor_pan_deg, anchor_tilt_deg

    def _ensure_anchor(self) -> tuple[float, float]:
        anchor_pan_deg = self._status.get("anchor_pan_deg")
        anchor_tilt_deg = self._status.get("anchor_tilt_deg")
        if anchor_pan_deg is not None and anchor_tilt_deg is not None:
            return float(anchor_pan_deg), float(anchor_tilt_deg)

        with self._robot_lock:
            anchor_pan_deg, anchor_tilt_deg = self._capture_anchor_locked()
        self._update_status(
            anchor_pan_deg=float(anchor_pan_deg),
            anchor_tilt_deg=float(anchor_tilt_deg),
            last_error="",
        )
        return float(anchor_pan_deg), float(anchor_tilt_deg)

    def _move_to_targets(
        self,
        *,
        targets_deg: dict[str, float],
        duration_sec: float,
        active_action: str,
    ) -> bool:
        clamped_targets = clamp_targets_deg(self._robot, targets_deg)
        with self._robot_lock:
            if self._stop_event.is_set():
                return False
            self._robot.move_joints(
                clamped_targets,
                duration=float(duration_sec),
                wait=False,
            )
        self._update_status(
            mode="performing",
            active_action=active_action,
            last_targets_deg={k: float(v) for k, v in clamped_targets.items()},
            last_command_duration_sec=float(duration_sec),
            last_error="",
        )
        return True

    def _center_to_anchor(self, *, active_action: str) -> bool:
        anchor_pan_deg, anchor_tilt_deg = self._ensure_anchor()
        duration_sec = self._scaled_duration(self._config.return_duration_sec)
        did_move = self._move_to_targets(
            targets_deg={
                str(self._config.pan_joint): float(anchor_pan_deg),
                str(self._config.tilt_joint): float(anchor_tilt_deg),
            },
            duration_sec=duration_sec,
            active_action=active_action,
        )
        if not did_move:
            return False
        if _sleep_or_stop(self._stop_event, duration_sec + float(self._config.settle_pause_sec)):
            return False
        return True

    def _nod_offsets(self) -> list[float]:
        offsets: list[float] = []
        amplitude_deg = abs(float(self._config.nod_amplitude_deg))
        cycles = max(1, int(self._config.nod_cycles))
        for index in range(cycles):
            major_scale = max(0.55, 1.0 - 0.18 * index)
            minor_scale = 0.45 if index == 0 else 0.30
            offsets.append(amplitude_deg * major_scale)
            offsets.append(-amplitude_deg * minor_scale)
        return offsets

    def _shake_offsets(self) -> list[float]:
        offsets: list[float] = []
        amplitude_deg = abs(float(self._config.shake_amplitude_deg))
        cycles = max(1, int(self._config.shake_cycles))
        for index in range(cycles):
            scale = max(0.65, 1.0 - 0.15 * index)
            offsets.append(amplitude_deg * scale)
            offsets.append(-amplitude_deg * scale)
        return offsets

    def _perform_joint_offsets(
        self,
        *,
        action: str,
        offsets_deg: list[float],
    ) -> bool:
        anchor_pan_deg, anchor_tilt_deg = self._ensure_anchor()
        beat_duration_sec = self._scaled_duration(self._config.beat_duration_sec)
        beat_pause_sec = float(self._config.beat_pause_sec)

        for offset_deg in offsets_deg:
            if action == "nod":
                targets_deg = {
                    str(self._config.pan_joint): float(anchor_pan_deg),
                    str(self._config.tilt_joint): float(anchor_tilt_deg) + float(offset_deg),
                }
            else:
                targets_deg = {
                    str(self._config.pan_joint): float(anchor_pan_deg) + float(offset_deg),
                    str(self._config.tilt_joint): float(anchor_tilt_deg),
                }
            did_move = self._move_to_targets(
                targets_deg=targets_deg,
                duration_sec=beat_duration_sec,
                active_action=action,
            )
            if not did_move:
                return False
            if _sleep_or_stop(self._stop_event, beat_duration_sec + beat_pause_sec):
                return False
        return True

    def _perform_action(self, action: str) -> None:
        normalized = _normalize_action_name(action)
        self._update_status(mode="performing", active_action=normalized, last_error="")

        if normalized == "reanchor":
            with self._robot_lock:
                anchor_pan_deg, anchor_tilt_deg = self._capture_anchor_locked()
            self._update_status(
                mode="idle",
                active_action="none",
                anchor_pan_deg=float(anchor_pan_deg),
                anchor_tilt_deg=float(anchor_tilt_deg),
                last_completed_action=normalized,
                completed_action_count=int(self._status.get("completed_action_count", 0) or 0) + 1,
                last_error="",
            )
            return

        if normalized == "center":
            if not self._center_to_anchor(active_action=normalized):
                return
        elif normalized == "nod":
            if not self._perform_joint_offsets(action=normalized, offsets_deg=self._nod_offsets()):
                return
            if bool(self._config.auto_center_after_action):
                if not self._center_to_anchor(active_action=normalized):
                    return
        elif normalized == "shake":
            if not self._perform_joint_offsets(action=normalized, offsets_deg=self._shake_offsets()):
                return
            if bool(self._config.auto_center_after_action):
                if not self._center_to_anchor(active_action=normalized):
                    return

        self._update_status(
            mode="idle",
            active_action="none",
            last_completed_action=normalized,
            completed_action_count=int(self._status.get("completed_action_count", 0) or 0) + 1,
            last_error="",
        )

    def _run(self) -> None:
        self._update_status(running=True, enabled=True, mode="idle")

        if bool(self._config.capture_anchor_on_start):
            try:
                with self._robot_lock:
                    anchor_pan_deg, anchor_tilt_deg = self._capture_anchor_locked()
                self._update_status(
                    anchor_pan_deg=float(anchor_pan_deg),
                    anchor_tilt_deg=float(anchor_tilt_deg),
                    last_error="",
                )
            except Exception as exc:  # noqa: BLE001
                self._update_status(last_error=f"{type(exc).__name__}: {exc}")

        while not self._stop_event.is_set():
            try:
                action = self._queue.get(timeout=max(0.02, float(self._config.poll_interval_sec)))
            except queue.Empty:
                continue

            if action == _STOP_SENTINEL:
                break

            try:
                self._perform_action(action)
            except Exception as exc:  # noqa: BLE001
                self._update_status(
                    mode="idle",
                    active_action="none",
                    last_error=f"{type(exc).__name__}: {exc}",
                )

        self._update_status(
            running=False,
            enabled=False,
            mode="stopped",
            active_action="none",
            queue_length=0,
        )


__all__ = [
    "ALLOWED_HAIGUITANG_ACTIONS",
    "DEFAULT_HAIGUITANG_PAN_JOINT",
    "DEFAULT_HAIGUITANG_TILT_JOINT",
    "HaiGuiTangConfig",
    "HaiGuiTangWorker",
    "build_default_haiguitang_payload",
]
