from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[5]
FACE_LOC_SRC = REPO_ROOT / "Software" / "Master" / "face_loc" / "src"
if FACE_LOC_SRC.exists():
    face_loc_src_str = str(FACE_LOC_SRC)
    face_loc_src_norm = os.path.normpath(face_loc_src_str)
    sys.path[:] = [
        path
        for path in sys.path
        if os.path.normpath(path or os.curdir) != face_loc_src_norm
    ]
    sys.path.insert(0, face_loc_src_str)

try:
    from face_tracking.target_center import get_target_center_norm
except Exception:  # noqa: BLE001
    def get_target_center_norm() -> tuple[float, float]:
        return (0.50, 0.42)


DEFAULT_LATEST_URL = "http://127.0.0.1:8000/latest"
DEFAULT_TARGET_KIND = "face"
DEFAULT_PAN_JOINT = "shoulder_pan"
DEFAULT_TILT_JOINT = "elbow_flex"
DEFAULT_PAN_SIGN = 1.0
DEFAULT_TILT_SIGN = 1.0
DEFAULT_PAN_GAIN_DEG_PER_NORM = 5.6
DEFAULT_TILT_GAIN_DEG_PER_NORM = 7.0
DEFAULT_PAN_DEAD_ZONE_NORM = 0.035
DEFAULT_TILT_DEAD_ZONE_NORM = 0.035
DEFAULT_PAN_RESUME_ZONE_NORM = 0.06
DEFAULT_TILT_RESUME_ZONE_NORM = 0.06
DEFAULT_MIN_PAN_STEP_DEG = 0.6
DEFAULT_MIN_TILT_STEP_DEG = 1.0
DEFAULT_PAN_MIN_STEP_ZONE_NORM = 0.09
DEFAULT_TILT_MIN_STEP_ZONE_NORM = 0.10
DEFAULT_MAX_PAN_STEP_DEG = 1.4
DEFAULT_MAX_TILT_STEP_DEG = 1.6
DEFAULT_MOVE_DURATION_S = 0.20
DEFAULT_POLL_INTERVAL_S = 0.08
DEFAULT_HTTP_TIMEOUT_S = 1.0
DEFAULT_COMMAND_MODE = "stream"
DEFAULT_LIMIT_MARGIN_RAW = 60
DEFAULT_STICTION_EPS_DEG = 0.15
DEFAULT_STICTION_FRAMES = 3
DEFAULT_PAN_BREAKAWAY_STEP_DEG = 1.8
DEFAULT_PAN_BREAKAWAY_STEP_NEG_DEG = 3.2
DEFAULT_PAN_NEGATIVE_SCALE = 1.45
DEFAULT_TILT_BREAKAWAY_STEP_DEG = 1.8


@dataclass
class AxisFollowState:
    active: bool = False
    offset_sign: int = 0
    last_joint_deg: float | None = None
    stagnant_frames: int = 0
    last_command_sign: int = 0


@dataclass
class FaceFollowConfig:
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
    pan_breakaway_step_pos: float | None = None
    pan_breakaway_step_neg: float = DEFAULT_PAN_BREAKAWAY_STEP_NEG_DEG
    pan_negative_scale: float = DEFAULT_PAN_NEGATIVE_SCALE
    tilt_breakaway_step: float = DEFAULT_TILT_BREAKAWAY_STEP_DEG


def build_default_follow_payload() -> dict[str, Any]:
    target_x_norm, target_y_norm = get_target_center_norm()
    config = FaceFollowConfig()
    return {
        "enabled": False,
        "running": False,
        "target_kind": config.target_kind,
        "latest_url": config.latest_url,
        "poll_interval_sec": float(config.poll_interval),
        "http_timeout_sec": float(config.http_timeout),
        "move_duration_sec": float(config.move_duration),
        "command_mode": str(config.command_mode),
        "pan_joint": str(config.pan_joint),
        "tilt_joint": str(config.tilt_joint),
        "pan_sign": float(config.pan_sign),
        "tilt_sign": float(config.tilt_sign),
        "target_visible": False,
        "last_frame_id": 0,
        "last_result_status": "",
        "last_error": "",
        "last_hold_reason": "",
        "last_limit_warning": "",
        "last_observation_age_ms": None,
        "last_target_center_norm": [float(target_x_norm), float(target_y_norm)],
        "last_face_center": None,
        "last_offset_norm": {"ndx": 0.0, "ndy": 0.0},
        "last_joint_step_deg": {},
        "started_at": None,
        "config": asdict(config),
    }


def _build_url_opener(target_url: str):
    if _should_bypass_proxy(target_url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _should_bypass_proxy(target_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(target_url)
    except ValueError:
        return False

    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost", "0.0.0.0", "::1"}:
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _fetch_latest(latest_url: str, timeout_s: float) -> dict[str, Any]:
    opener = _build_url_opener(latest_url)
    with opener.open(latest_url, timeout=max(0.1, float(timeout_s))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected /latest payload type: {type(payload).__name__}")
    return payload


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _compute_joint_step(
    *,
    axis_state: AxisFollowState,
    normalized_offset: float,
    gain_deg_per_norm: float,
    dead_zone_norm: float,
    resume_zone_norm: float,
    min_step_deg: float,
    min_step_zone_norm: float,
    max_step_deg: float,
    sign: float,
) -> float:
    offset_value = float(normalized_offset)
    offset_abs = abs(offset_value)
    offset_sign = _sign(offset_value)
    dead_zone = abs(float(dead_zone_norm))
    resume_zone = max(dead_zone, abs(float(resume_zone_norm)))

    if axis_state.active:
        if offset_abs <= dead_zone:
            axis_state.active = False
            axis_state.offset_sign = 0
            return 0.0
        if offset_sign != 0 and offset_sign != axis_state.offset_sign and offset_abs < resume_zone:
            axis_state.active = False
            axis_state.offset_sign = 0
            return 0.0
    elif offset_abs < resume_zone:
        return 0.0

    axis_state.active = True
    axis_state.offset_sign = offset_sign

    raw_step_deg = offset_value * float(gain_deg_per_norm) * float(sign)
    min_step_abs = abs(float(min_step_deg))
    min_step_zone = max(dead_zone, abs(float(min_step_zone_norm)))
    if 0.0 < abs(raw_step_deg) < min_step_abs and offset_abs >= min_step_zone:
        raw_step_deg = min_step_abs if raw_step_deg > 0.0 else -min_step_abs
    return _clamp(raw_step_deg, -abs(float(max_step_deg)), abs(float(max_step_deg)))


def _reset_axis_state(axis_state: AxisFollowState) -> None:
    axis_state.active = False
    axis_state.offset_sign = 0
    axis_state.last_joint_deg = None
    axis_state.stagnant_frames = 0
    axis_state.last_command_sign = 0


def _apply_stiction_breakaway(
    axis_state: AxisFollowState,
    *,
    current_joint_deg: float,
    step_deg: float,
    movement_eps_deg: float,
    stagnant_frame_threshold: int,
    breakaway_step_pos_deg: float,
    breakaway_step_neg_deg: float,
) -> tuple[float, float | None]:
    step_sign = _sign(step_deg)
    measured_delta_deg: float | None = None
    if axis_state.last_joint_deg is not None:
        measured_delta_deg = float(current_joint_deg) - float(axis_state.last_joint_deg)

    if step_sign == 0:
        axis_state.stagnant_frames = 0
        axis_state.last_command_sign = 0
        axis_state.last_joint_deg = float(current_joint_deg)
        return 0.0, measured_delta_deg

    moved_enough = measured_delta_deg is not None and abs(float(measured_delta_deg)) >= abs(float(movement_eps_deg))
    if axis_state.last_command_sign == step_sign and not moved_enough:
        axis_state.stagnant_frames += 1
    else:
        axis_state.stagnant_frames = 0

    adjusted_step_deg = float(step_deg)
    if axis_state.stagnant_frames >= max(1, int(stagnant_frame_threshold)):
        directional_breakaway_abs = (
            abs(float(breakaway_step_pos_deg))
            if step_sign > 0
            else abs(float(breakaway_step_neg_deg))
        )
        boosted_abs = max(abs(float(step_deg)), directional_breakaway_abs)
        adjusted_step_deg = boosted_abs if step_sign > 0 else -boosted_abs
    elif axis_state.stagnant_frames > 0:
        directional_breakaway_abs = (
            abs(float(breakaway_step_pos_deg))
            if step_sign > 0
            else abs(float(breakaway_step_neg_deg))
        )
        base_abs = abs(float(step_deg))
        if directional_breakaway_abs > base_abs:
            ramp_ratio = min(
                1.0,
                float(axis_state.stagnant_frames) / float(max(1, int(stagnant_frame_threshold))),
            )
            boosted_abs = base_abs + (directional_breakaway_abs - base_abs) * ramp_ratio
            adjusted_step_deg = boosted_abs if step_sign > 0 else -boosted_abs

    axis_state.last_command_sign = step_sign
    axis_state.last_joint_deg = float(current_joint_deg)
    return adjusted_step_deg, measured_delta_deg


def _extract_target_center_norm(result: dict[str, Any]) -> tuple[float, float]:
    payload = result.get("target_center")
    if isinstance(payload, dict):
        try:
            return (float(payload["x_norm"]), float(payload["y_norm"]))
        except (KeyError, TypeError, ValueError):
            pass
    return get_target_center_norm()


def _single_turn_limit_warning(
    robot: Any,
    current_state: Mapping[str, Any],
    *,
    joint_name: str,
    delta_deg: float,
    limit_margin_raw: int,
) -> str | None:
    controller = getattr(robot, "_controller", None)
    calibration_payload = getattr(controller, "_calibration_payload", None)
    if not isinstance(calibration_payload, Mapping):
        return None

    calibration_entry = calibration_payload.get(joint_name)
    if not isinstance(calibration_entry, Mapping):
        return None

    raw_present = current_state.get("raw_present_position")
    if not isinstance(raw_present, Mapping) or joint_name not in raw_present:
        return None

    joint_deg_to_relative_raw = getattr(controller, "_joint_deg_to_relative_raw", None)
    if not callable(joint_deg_to_relative_raw):
        return None

    try:
        range_min = int(calibration_entry["range_min"])
        range_max = int(calibration_entry["range_max"])
        present_raw = int(raw_present[joint_name])
        relative_delta_raw = float(joint_deg_to_relative_raw(joint_name, float(delta_deg)))
    except (KeyError, TypeError, ValueError):
        return None

    margin = max(0, int(limit_margin_raw))
    if relative_delta_raw > 0.0 and present_raw >= range_max - margin:
        return (
            f"{joint_name} may be at its positive single-turn limit: "
            f"present_raw={present_raw}, range_max={range_max}, delta_deg={float(delta_deg):+.2f}"
        )
    if relative_delta_raw < 0.0 and present_raw <= range_min + margin:
        return (
            f"{joint_name} may be at its negative single-turn limit: "
            f"present_raw={present_raw}, range_min={range_min}, delta_deg={float(delta_deg):+.2f}"
        )
    return None


class FaceFollowWorker:
    def __init__(
        self,
        *,
        robot: Any,
        robot_lock: threading.RLock,
        config: FaceFollowConfig,
    ) -> None:
        self._robot = robot
        self._robot_lock = robot_lock
        self._config = config
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status = build_default_follow_payload()
        self._status["enabled"] = True
        self._status["running"] = False
        self._status["target_kind"] = str(config.target_kind)
        self._status["latest_url"] = str(config.latest_url)
        self._status["poll_interval_sec"] = float(config.poll_interval)
        self._status["http_timeout_sec"] = float(config.http_timeout)
        self._status["move_duration_sec"] = float(config.move_duration)
        self._status["command_mode"] = str(config.command_mode)
        self._status["pan_joint"] = str(config.pan_joint)
        self._status["tilt_joint"] = str(config.tilt_joint)
        self._status["pan_sign"] = float(config.pan_sign)
        self._status["tilt_sign"] = float(config.tilt_sign)
        self._status["started_at"] = time.time()
        self._status["config"] = asdict(config)
        self._last_observation_monotonic = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name="QuickControlFaceFollow",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()
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
            return payload

    def _sleep(self) -> None:
        self._stop_event.wait(max(0.01, float(self._config.poll_interval)))

    def _update_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _run(self) -> None:
        pan_axis_state = AxisFollowState()
        tilt_axis_state = AxisFollowState()
        last_frame_id = -1
        self._update_status(running=True, enabled=True)

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
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                self._update_status(
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
                self._update_status(
                    target_visible=False,
                    last_hold_reason=f"status={result_status}",
                    last_joint_step_deg={},
                    last_limit_warning="",
                )
                self._sleep()
                continue

            offset_payload = result.get("smoothed_offset") or result.get("offset")
            if not isinstance(offset_payload, dict):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
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
            except (KeyError, TypeError, ValueError):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
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
                pan_step_deg = _clamp(
                    pan_step_deg * negative_scale,
                    -abs(float(self._config.max_pan_step)) * negative_scale,
                    abs(float(self._config.max_pan_step)),
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
            face_center = (
                target_face.get("center")
                if isinstance(target_face, dict)
                else None
            )
            self._last_observation_monotonic = time.monotonic()
            self._update_status(
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

            if self._stop_event.is_set():
                break

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
                    last_joint_step_deg={joint_name: float(delta_deg) for joint_name, delta_deg in joint_targets.items()},
                )
            except Exception as exc:  # noqa: BLE001
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                self._update_status(
                    target_visible=False,
                    last_error=f"{type(exc).__name__}: {exc}",
                    last_hold_reason=f"hardware command failed: {type(exc).__name__}: {exc}",
                    last_joint_step_deg={},
                )

            self._sleep()

        self._update_status(running=False, enabled=False)


__all__ = [
    "DEFAULT_COMMAND_MODE",
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_LATEST_URL",
    "DEFAULT_MOVE_DURATION_S",
    "DEFAULT_PAN_JOINT",
    "DEFAULT_PAN_SIGN",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TARGET_KIND",
    "DEFAULT_TILT_JOINT",
    "DEFAULT_TILT_SIGN",
    "FaceFollowConfig",
    "FaceFollowWorker",
    "build_default_follow_payload",
]
