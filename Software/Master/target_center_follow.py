#!/usr/bin/env python3
"""Keep an arbitrary target center near the camera center using SoarmMoce SDK."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import Robot


SIGN_CACHE_PATH = Path(__file__).resolve().parent / "calibration" / "target_center_follow_signs.json"
CONTROLLER_SIGN_CACHE_PATH = Path(__file__).resolve().parent / "calibration" / "target_center_follow_controller_signs.json"
TARGET_CONTAINER_KEYS = (
    "target",
    "target_face",
    "target_object",
    "focus_target",
    "interesting_target",
    "point_target",
    "best_target",
)
SMOOTHED_TARGET_CONTAINER_KEYS = (
    "smoothed_target",
    "smoothed_target_face",
    "smoothed_focus_target",
    "smoothed_point_target",
)
CENTER_KEYS = ("center", "target_center", "focus_point", "point")
NORMALIZED_CENTER_KEYS = ("center_norm", "normalized_center", "target_center_norm", "focus_point_norm", "point_norm")


class ValidationError(ValueError):
    """Raised when the script arguments or payload schema are invalid."""


class TargetPayloadError(RuntimeError):
    """Raised when the target payload is missing or unusable."""


def _log(message: str) -> None:
    print(f"[target-center-follow] {message}", file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    print(f"[target-center-follow][warn] {message}", file=sys.stderr, flush=True)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def _normalize_optional_joint(raw: str | None, allowed: set[str]) -> Optional[str]:
    value = str(raw or "").strip().lower()
    if value in {"", "none", "off", "disable", "disabled"}:
        return None
    if value not in allowed:
        raise ValidationError(f"Unknown joint: {raw}")
    return value


def _normalize_sign_arg(raw: str | float | int | None, flag_name: str) -> Optional[float]:
    value = str(raw or "").strip().lower()
    if value in {"", "auto"}:
        return None
    if value in {"1", "+1", "positive", "pos"}:
        return 1.0
    if value in {"-1", "negative", "neg"}:
        return -1.0
    raise ValidationError(f"{flag_name} must be one of: auto, 1, -1")


def _normalize_probe_policy(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "skip-optional"}:
        return "skip-optional"
    if value in {"strict", "disable-axis", "skip-optional"}:
        return value
    raise ValidationError("--probe-failure-policy must be one of: strict, disable-axis, skip-optional")


def _load_sign_cache(path: Path = SIGN_CACHE_PATH) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        _warn(f"ignore invalid sign cache: {path}")
        return {}
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, raw_value in payload.items():
        try:
            sign = float(raw_value)
        except (TypeError, ValueError):
            continue
        if sign in {-1.0, 1.0}:
            normalized[str(key)] = sign
    return normalized


def _save_sign_cache(signs: dict[str, float], path: Path = SIGN_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: float(value) for key, value in sorted(signs.items()) if float(value) in {-1.0, 1.0}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fetch_json(url: str, timeout_sec: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=float(timeout_sec)) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} when requesting {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to connect to {url}: {exc}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response from {url}: expected JSON object")
    return data


class TargetPointClient:
    def __init__(self, endpoint: str, timeout_sec: float) -> None:
        base = str(endpoint).strip().rstrip("/")
        if not base:
            raise ValidationError("--point-endpoint is required")
        if base.endswith("/latest"):
            service_base = base[:-7]
            self.latest_url = base
        else:
            service_base = base
            self.latest_url = base + "/latest"
        self.status_url = service_base + "/status"
        self.timeout_sec = float(timeout_sec)

    def get_latest(self) -> dict[str, Any]:
        return _fetch_json(self.latest_url, self.timeout_sec)

    def get_status(self) -> dict[str, Any]:
        return _fetch_json(self.status_url, self.timeout_sec)


@dataclass(slots=True)
class JointAxis:
    joint_name: str
    joint_index: int
    metric_key: str
    gain_rad_per_norm: float
    max_step_rad: float
    dead_zone_norm: float
    min_rad: float
    max_rad: float
    control_sign: float = 0.0

    def compute_next_target(self, current_joint_rad: float, normalized_error: float) -> Optional[float]:
        if abs(normalized_error) <= self.dead_zone_norm:
            return None
        raw_delta = self.control_sign * self.gain_rad_per_norm * normalized_error
        delta_rad = _clamp(raw_delta, -self.max_step_rad, self.max_step_rad)
        if abs(delta_rad) < 1e-9:
            return None
        return _clamp(current_joint_rad + delta_rad, self.min_rad, self.max_rad)


@dataclass(slots=True)
class TargetObservation:
    frame_id: int
    timestamp: float
    ndx: float
    ndy: float
    raw_ndx: float
    raw_ndy: float
    payload: dict[str, Any]
    frame_size: tuple[int, int] | None = None
    center: tuple[float, float] | None = None


@dataclass(slots=True)
class ControllerJointAxis:
    joint_name: str
    metric_key: str
    gain_deg_per_norm: float
    max_step_deg: float
    dead_zone_norm: float
    min_deg: float
    max_deg: float
    control_sign: float = 0.0

    def compute_next_target(self, current_joint_deg: float, normalized_error: float) -> Optional[float]:
        if abs(normalized_error) <= self.dead_zone_norm:
            return None
        raw_delta = self.control_sign * self.gain_deg_per_norm * normalized_error
        delta_deg = _clamp(raw_delta, -self.max_step_deg, self.max_step_deg)
        if abs(delta_deg) < 1e-6:
            return None
        return _clamp(current_joint_deg + delta_deg, self.min_deg, self.max_deg)


def _deg_to_rad(value_deg: float) -> float:
    return float(np.deg2rad(float(value_deg)))


def _rad_to_deg(value_rad: float) -> float:
    return float(np.rad2deg(float(value_rad)))


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_point_pair(raw: Any) -> tuple[float, float] | None:
    if isinstance(raw, dict):
        if _is_numeric(raw.get("x")) and _is_numeric(raw.get("y")):
            return float(raw["x"]), float(raw["y"])
        if _is_numeric(raw.get("cx")) and _is_numeric(raw.get("cy")):
            return float(raw["cx"]), float(raw["cy"])
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2 and _is_numeric(raw[0]) and _is_numeric(raw[1]):
        return float(raw[0]), float(raw[1])
    return None


def _parse_frame_size(raw: Any) -> tuple[int, int] | None:
    pair = _parse_point_pair(raw)
    if pair is not None:
        width, height = pair
        if width > 0.0 and height > 0.0:
            return int(round(width)), int(round(height))
        return None
    if isinstance(raw, dict) and _is_numeric(raw.get("width")) and _is_numeric(raw.get("height")):
        width = int(round(float(raw["width"])))
        height = int(round(float(raw["height"])))
        if width > 0 and height > 0:
            return width, height
    return None


def _extract_frame_size(payload: dict[str, Any]) -> tuple[int, int] | None:
    for key in ("frame_size", "image_size", "size"):
        parsed = _parse_frame_size(payload.get(key))
        if parsed is not None:
            return parsed
    if _is_numeric(payload.get("width")) and _is_numeric(payload.get("height")):
        width = int(round(float(payload["width"])))
        height = int(round(float(payload["height"])))
        if width > 0 and height > 0:
            return width, height
    return None


def _candidate_containers(payload: dict[str, Any], *, smoothed: bool) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = [payload]
    keys = SMOOTHED_TARGET_CONTAINER_KEYS if smoothed else TARGET_CONTAINER_KEYS
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            containers.append(candidate)
    return containers


def _extract_center_pair(payload: dict[str, Any], *, smoothed: bool) -> tuple[float, float] | None:
    containers = _candidate_containers(payload, smoothed=smoothed)
    for container in containers:
        for key in CENTER_KEYS:
            parsed = _parse_point_pair(container.get(key))
            if parsed is not None:
                return parsed
    return None


def _extract_normalized_center(payload: dict[str, Any], *, smoothed: bool) -> tuple[float, float] | None:
    containers = _candidate_containers(payload, smoothed=smoothed)
    for container in containers:
        for key in NORMALIZED_CENTER_KEYS:
            parsed = _parse_point_pair(container.get(key))
            if parsed is not None:
                return parsed
    return None


def _compute_norm_from_center(center: tuple[float, float], frame_size: tuple[int, int]) -> tuple[float, float]:
    frame_width, frame_height = frame_size
    half_width = max(float(frame_width) / 2.0, 1.0)
    half_height = max(float(frame_height) / 2.0, 1.0)
    ndx = (float(center[0]) - half_width) / half_width
    ndy = (float(center[1]) - half_height) / half_height
    return float(ndx), float(ndy)


def _compute_norm_from_normalized_center(center_norm: tuple[float, float]) -> tuple[float, float]:
    cx, cy = center_norm
    return float((cx - 0.5) * 2.0), float((cy - 0.5) * 2.0)


def _extract_offset_pair(payload: dict[str, Any], key: str) -> tuple[float, float] | None:
    candidate = payload.get(key)
    if not isinstance(candidate, dict):
        return None
    if _is_numeric(candidate.get("ndx")) and _is_numeric(candidate.get("ndy")):
        return float(candidate["ndx"]), float(candidate["ndy"])
    frame_size = _extract_frame_size(payload)
    if frame_size is not None and _is_numeric(candidate.get("dx")) and _is_numeric(candidate.get("dy")):
        half_width = max(float(frame_size[0]) / 2.0, 1.0)
        half_height = max(float(frame_size[1]) / 2.0, 1.0)
        return float(candidate["dx"]) / half_width, float(candidate["dy"]) / half_height
    return None


def extract_target_observation(payload: dict[str, Any]) -> TargetObservation:
    if not isinstance(payload, dict):
        raise TargetPayloadError("payload must be a JSON object")
    if payload.get("ok") is False:
        raise TargetPayloadError(str(payload.get("error") or "payload reports ok=false"))
    if "detected" in payload and not bool(payload.get("detected")):
        raise TargetPayloadError("detected=false")
    if "found" in payload and not bool(payload.get("found")):
        raise TargetPayloadError("found=false")

    frame_id = int(payload.get("frame_id") or 0)
    timestamp = float(payload.get("timestamp") or time.time())
    frame_size = _extract_frame_size(payload)

    raw_offset = _extract_offset_pair(payload, "offset")
    smoothed_offset = _extract_offset_pair(payload, "smoothed_offset")

    raw_center = _extract_center_pair(payload, smoothed=False)
    smoothed_center = _extract_center_pair(payload, smoothed=True)
    raw_center_norm = _extract_normalized_center(payload, smoothed=False)
    smoothed_center_norm = _extract_normalized_center(payload, smoothed=True)

    if raw_offset is None:
        if raw_center_norm is not None:
            raw_offset = _compute_norm_from_normalized_center(raw_center_norm)
        elif raw_center is not None and frame_size is not None:
            raw_offset = _compute_norm_from_center(raw_center, frame_size)

    if smoothed_offset is None:
        if smoothed_center_norm is not None:
            smoothed_offset = _compute_norm_from_normalized_center(smoothed_center_norm)
        elif smoothed_center is not None and frame_size is not None:
            smoothed_offset = _compute_norm_from_center(smoothed_center, frame_size)

    if raw_offset is None and smoothed_offset is None:
        status = str(payload.get("status") or "").strip() or "unknown"
        raise TargetPayloadError(f"payload does not contain a usable target center or offset (status={status})")

    active_raw = raw_offset or smoothed_offset
    active_smoothed = smoothed_offset or raw_offset
    if active_raw is None or active_smoothed is None:
        raise TargetPayloadError("payload missing usable offsets")

    return TargetObservation(
        frame_id=frame_id,
        timestamp=timestamp,
        ndx=float(active_smoothed[0]),
        ndy=float(active_smoothed[1]),
        raw_ndx=float(active_raw[0]),
        raw_ndy=float(active_raw[1]),
        payload=payload,
        frame_size=frame_size,
        center=smoothed_center or raw_center,
    )


def _wait_for_target(
    client: TargetPointClient,
    *,
    timeout_sec: float,
    max_staleness_sec: float,
    newer_than_frame_id: Optional[int] = None,
) -> TargetObservation:
    deadline = time.time() + max(0.1, float(timeout_sec))
    last_problem = "target service did not return a usable payload"
    while time.time() < deadline:
        payload = client.get_latest()
        try:
            observation = extract_target_observation(payload)
        except TargetPayloadError as exc:
            last_problem = str(exc)
            time.sleep(0.05)
            continue
        age_sec = max(0.0, time.time() - float(observation.timestamp))
        if age_sec > max_staleness_sec:
            last_problem = f"stale target payload: age={age_sec:.2f}s"
            time.sleep(0.05)
            continue
        if newer_than_frame_id is not None and int(observation.frame_id) <= newer_than_frame_id:
            last_problem = f"waiting for a newer frame than {newer_than_frame_id}"
            time.sleep(0.05)
            continue
        return observation
    raise RuntimeError(last_problem)


def _collect_metric_median(
    client: TargetPointClient,
    *,
    metric_key: str,
    sample_count: int,
    timeout_sec: float,
    max_staleness_sec: float,
    newer_than_frame_id: Optional[int] = None,
    use_raw_metric: bool,
) -> tuple[float, TargetObservation]:
    samples: list[float] = []
    latest_observation: TargetObservation | None = None
    last_frame_id = newer_than_frame_id
    for _ in range(max(1, int(sample_count))):
        observation = _wait_for_target(
            client,
            timeout_sec=timeout_sec,
            max_staleness_sec=max_staleness_sec,
            newer_than_frame_id=last_frame_id,
        )
        last_frame_id = int(observation.frame_id)
        latest_observation = observation
        if metric_key == "ndx":
            samples.append(float(observation.raw_ndx if use_raw_metric else observation.ndx))
        elif metric_key == "ndy":
            samples.append(float(observation.raw_ndy if use_raw_metric else observation.ndy))
        else:
            raise ValidationError(f"Unsupported metric key: {metric_key}")
    if latest_observation is None:
        raise RuntimeError("failed to collect target observations")
    return float(statistics.median(samples)), latest_observation


def _move_q(robot: Robot, q_target: np.ndarray, *, duration_sec: float, wait: bool) -> np.ndarray:
    q_arr = np.asarray(q_target, dtype=float).reshape(-1)
    robot.move_joints(q_arr, duration=float(duration_sec), wait=bool(wait))
    return q_arr


def _probe_axis_sign(
    robot: Robot,
    client: TargetPointClient,
    axis: JointAxis,
    *,
    probe_delta_rad: float,
    move_duration_sec: float,
    point_timeout_sec: float,
    max_point_staleness_sec: float,
    min_probe_metric_delta: float,
) -> dict[str, Any]:
    baseline_metric, baseline_obs = _collect_metric_median(
        client,
        metric_key=axis.metric_key,
        sample_count=3,
        timeout_sec=point_timeout_sec,
        max_staleness_sec=max_point_staleness_sec,
        use_raw_metric=True,
    )

    base_q = robot.get_joint_state().q.copy()
    probe_multipliers = [1.0, 1.75, 2.5]
    last_result: dict[str, Any] | None = None
    for multiplier in probe_multipliers:
        target_q = np.asarray(base_q, dtype=float).copy()
        requested_delta = float(probe_delta_rad) * float(multiplier)
        target_q[axis.joint_index] = _clamp(
            float(base_q[axis.joint_index]) + requested_delta,
            axis.min_rad,
            axis.max_rad,
        )
        actual_delta = float(target_q[axis.joint_index] - base_q[axis.joint_index])
        if abs(actual_delta) < 1e-9:
            continue

        _log(
            f"probing {axis.joint_name} on {axis.metric_key}: baseline={baseline_metric:+.4f}, "
            f"joint+={_rad_to_deg(actual_delta):.3f}deg"
        )
        _move_q(robot, target_q, duration_sec=move_duration_sec, wait=True)
        moved_obs: TargetObservation | None = None
        try:
            moved_metric, moved_obs = _collect_metric_median(
                client,
                metric_key=axis.metric_key,
                sample_count=3,
                timeout_sec=point_timeout_sec,
                max_staleness_sec=max_point_staleness_sec,
                newer_than_frame_id=int(baseline_obs.frame_id),
                use_raw_metric=True,
            )
        finally:
            _move_q(robot, base_q, duration_sec=move_duration_sec, wait=True)
            newer_than = int(moved_obs.frame_id) if moved_obs is not None else int(baseline_obs.frame_id)
            try:
                _wait_for_target(
                    client,
                    timeout_sec=point_timeout_sec,
                    max_staleness_sec=max_point_staleness_sec,
                    newer_than_frame_id=newer_than,
                )
            except Exception:
                pass

        metric_delta = float(moved_metric - baseline_metric)
        last_result = {
            "joint": axis.joint_name,
            "metric": axis.metric_key,
            "probe_delta_deg": _rad_to_deg(actual_delta),
            "baseline_metric": float(baseline_metric),
            "moved_metric": float(moved_metric),
            "metric_delta": float(metric_delta),
            "control_sign": float(-1.0 if metric_delta > 0.0 else 1.0),
        }
        if abs(metric_delta) >= float(min_probe_metric_delta):
            axis.control_sign = float(last_result["control_sign"])
            return last_result
        _warn(
            f"probe too weak on {axis.joint_name}: delta={metric_delta:+.5f} with "
            f"{_rad_to_deg(actual_delta):.3f}deg; trying a larger probe"
        )

    if last_result is None:
        raise RuntimeError(f"Probe on {axis.joint_name} did not produce any usable measurement")
    raise RuntimeError(
        f"Probe on {axis.joint_name} changed {axis.metric_key} by only {last_result['metric_delta']:+.5f} "
        f"even after probing up to {last_result['probe_delta_deg']:.3f}deg"
    )


def _build_axis(
    *,
    robot: Robot,
    joint_name: Optional[str],
    metric_key: str,
    gain_deg_per_norm: float,
    max_step_deg: float,
    dead_zone_norm: float,
    current_joint_rad: float,
    range_deg: float,
) -> Optional[JointAxis]:
    if joint_name is None:
        return None
    joint_index = robot.robot_model.resolve_joint_index(joint_name)
    model_min, model_max = robot.robot_model.joint_limits[joint_index]
    span = _deg_to_rad(max(0.5, float(range_deg)))
    min_rad = float(current_joint_rad) - span
    max_rad = float(current_joint_rad) + span
    if np.isfinite(model_min):
        min_rad = max(min_rad, float(model_min))
    if np.isfinite(model_max):
        max_rad = min(max_rad, float(model_max))
    return JointAxis(
        joint_name=joint_name,
        joint_index=int(joint_index),
        metric_key=metric_key,
        gain_rad_per_norm=_deg_to_rad(gain_deg_per_norm),
        max_step_rad=_deg_to_rad(max_step_deg),
        dead_zone_norm=max(0.0, float(dead_zone_norm)),
        min_rad=float(min_rad),
        max_rad=float(max_rad),
    )


def _apply_joint_sign(
    *,
    axis: JointAxis | None,
    axis_key: str,
    explicit_sign: Optional[float],
    use_cache: bool,
    sign_cache: dict[str, float],
    calibration: list[dict[str, Any]],
) -> bool:
    if axis is None:
        return True
    if explicit_sign is not None:
        axis.control_sign = float(explicit_sign)
        calibration.append(
            {
                "joint": axis.joint_name,
                "metric": axis.metric_key,
                "control_sign": axis.control_sign,
                "mode": "manual",
                "cache_key": axis_key,
            }
        )
        _log(f"axis sign fixed: joint={axis.joint_name} control_sign={axis.control_sign:+.1f}")
        return True
    if use_cache and axis_key in sign_cache:
        axis.control_sign = float(sign_cache[axis_key])
        calibration.append(
            {
                "joint": axis.joint_name,
                "metric": axis.metric_key,
                "control_sign": axis.control_sign,
                "mode": "cache",
                "cache_key": axis_key,
            }
        )
        _log(f"axis sign cached: joint={axis.joint_name} control_sign={axis.control_sign:+.1f}")
        return True
    return False


def _apply_search_step(
    robot: Robot,
    *,
    current_q: np.ndarray,
    pan_axis: JointAxis | None,
    search_state: dict[str, Any],
    move_duration_sec: float,
    wait_for_motion: bool,
) -> tuple[np.ndarray | None, bool]:
    if pan_axis is None:
        return None, False
    direction = float(search_state.get("direction", 1.0))
    step_rad = _deg_to_rad(float(search_state.get("step_deg", 2.0)))
    min_rad = float(search_state.get("min_rad", pan_axis.min_rad))
    max_rad = float(search_state.get("max_rad", pan_axis.max_rad))
    current = float(current_q[pan_axis.joint_index])
    target = current + direction * step_rad

    bounced = False
    if target > max_rad:
        direction = -1.0
        target = max_rad
        bounced = True
    elif target < min_rad:
        direction = 1.0
        target = min_rad
        bounced = True

    if abs(target - current) < 1e-9:
        search_state["direction"] = -direction
        return None, False

    q_target = np.asarray(current_q, dtype=float).copy()
    q_target[pan_axis.joint_index] = target
    _move_q(robot, q_target, duration_sec=move_duration_sec, wait=wait_for_motion)
    search_state["direction"] = direction if not bounced else -direction
    search_state["steps"] = int(search_state.get("steps", 0)) + 1
    return q_target, True


def _handle_probe_failure(
    *,
    axis_label: str,
    axis_optional: bool,
    policy: str,
    exc: Exception,
) -> bool:
    if policy == "strict":
        raise exc
    if policy == "disable-axis":
        _warn(f"disable {axis_label}: {exc}")
        return True
    if axis_optional:
        _warn(f"skip optional {axis_label}: {exc}")
        return True
    raise exc


def _joint_state_deg_payload(robot: Robot, q: np.ndarray) -> dict[str, float]:
    q_arr = np.asarray(q, dtype=float).reshape(-1)
    return {
        name: round(_rad_to_deg(q_arr[idx]), 6)
        for idx, name in enumerate(robot.robot_model.joint_names)
    }


_LOCAL_CONTROLLER_MODULE = None


def _load_local_controller_module():
    global _LOCAL_CONTROLLER_MODULE
    if _LOCAL_CONTROLLER_MODULE is not None:
        return _LOCAL_CONTROLLER_MODULE
    module_path = Path(__file__).resolve().parent / "soarmmoce_sdk.py"
    if not module_path.exists():
        raise RuntimeError(f"Local controller backend not found: {module_path}")
    module_name = "software_master_soarmmoce_sdk_runtime"
    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load local controller backend: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _LOCAL_CONTROLLER_MODULE = module
    return module


def _build_controller_axis(
    *,
    joint_name: Optional[str],
    metric_key: str,
    gain_deg_per_norm: float,
    max_step_deg: float,
    dead_zone_norm: float,
    current_joint_deg: float,
    range_deg: float,
) -> Optional[ControllerJointAxis]:
    if joint_name is None:
        return None
    span = max(0.5, float(range_deg))
    return ControllerJointAxis(
        joint_name=joint_name,
        metric_key=metric_key,
        gain_deg_per_norm=float(gain_deg_per_norm),
        max_step_deg=max(0.1, float(max_step_deg)),
        dead_zone_norm=max(0.0, float(dead_zone_norm)),
        min_deg=float(current_joint_deg) - span,
        max_deg=float(current_joint_deg) + span,
    )


def _probe_controller_axis_sign(
    arm: Any,
    client: TargetPointClient,
    axis: ControllerJointAxis,
    *,
    probe_delta_deg: float,
    move_duration_sec: float,
    point_timeout_sec: float,
    max_point_staleness_sec: float,
    min_probe_metric_delta: float,
) -> dict[str, Any]:
    baseline_metric, baseline_obs = _collect_metric_median(
        client,
        metric_key=axis.metric_key,
        sample_count=3,
        timeout_sec=point_timeout_sec,
        max_staleness_sec=max_point_staleness_sec,
        use_raw_metric=True,
    )

    probe_multipliers = [1.0, 1.75, 2.5]
    last_result: dict[str, Any] | None = None
    for multiplier in probe_multipliers:
        effective_probe_delta = float(probe_delta_deg) * float(multiplier)
        _log(
            f"probing {axis.joint_name} on {axis.metric_key}: baseline={baseline_metric:+.4f}, "
            f"joint+={effective_probe_delta:.3f}deg"
        )
        move_plus = arm.move_joint(
            joint=axis.joint_name,
            delta_deg=effective_probe_delta,
            duration=move_duration_sec,
            wait=True,
        )
        moved_obs: TargetObservation | None = None
        revert: dict[str, Any] | None = None
        try:
            moved_metric, moved_obs = _collect_metric_median(
                client,
                metric_key=axis.metric_key,
                sample_count=3,
                timeout_sec=point_timeout_sec,
                max_staleness_sec=max_point_staleness_sec,
                newer_than_frame_id=int(baseline_obs.frame_id),
                use_raw_metric=True,
            )
        finally:
            revert = arm.move_joint(
                joint=axis.joint_name,
                delta_deg=-effective_probe_delta,
                duration=move_duration_sec,
                wait=True,
            )
            newer_than = int(moved_obs.frame_id) if moved_obs is not None else int(baseline_obs.frame_id)
            try:
                _wait_for_target(
                    client,
                    timeout_sec=point_timeout_sec,
                    max_staleness_sec=max_point_staleness_sec,
                    newer_than_frame_id=newer_than,
                )
            except Exception:
                pass

        metric_delta = float(moved_metric - baseline_metric)
        last_result = {
            "joint": axis.joint_name,
            "metric": axis.metric_key,
            "probe_delta_deg": float(effective_probe_delta),
            "baseline_metric": float(baseline_metric),
            "moved_metric": float(moved_metric),
            "metric_delta": float(metric_delta),
            "control_sign": float(-1.0 if metric_delta > 0.0 else 1.0),
            "revert_joint_state": revert["state"]["joint_state"] if revert is not None else {},
            "move_joint_state": move_plus["state"]["joint_state"],
        }
        if abs(metric_delta) >= float(min_probe_metric_delta):
            axis.control_sign = float(last_result["control_sign"])
            return last_result
        _warn(
            f"probe too weak on {axis.joint_name}: delta={metric_delta:+.5f} with "
            f"{effective_probe_delta:.3f}deg; trying a larger probe"
        )

    if last_result is None:
        raise RuntimeError(f"Probe on {axis.joint_name} did not produce any usable measurement")
    raise RuntimeError(
        f"Probe on {axis.joint_name} changed {axis.metric_key} by only {last_result['metric_delta']:+.5f} "
        f"even after probing up to {last_result['probe_delta_deg']:.3f}deg"
    )


def _apply_controller_search_step(
    arm: Any,
    *,
    current_joint_state: dict[str, float],
    pan_axis: ControllerJointAxis | None,
    search_state: dict[str, Any],
    move_duration_sec: float,
    wait_for_motion: bool,
) -> tuple[dict[str, Any] | None, bool]:
    if pan_axis is None:
        return None, False

    direction = float(search_state.get("direction", 1.0))
    step_deg = float(search_state.get("step_deg", 2.0))
    min_deg = float(search_state.get("min_deg", pan_axis.min_deg))
    max_deg = float(search_state.get("max_deg", pan_axis.max_deg))
    current = float(current_joint_state[pan_axis.joint_name])
    target = current + direction * step_deg

    bounced = False
    if target > max_deg:
        direction = -1.0
        target = max_deg
        bounced = True
    elif target < min_deg:
        direction = 1.0
        target = min_deg
        bounced = True

    if abs(target - current) < 1e-6:
        search_state["direction"] = -direction
        return None, False

    result = arm.move_joint(
        joint=pan_axis.joint_name,
        target_deg=target,
        duration=move_duration_sec,
        wait=wait_for_motion,
    )
    search_state["direction"] = direction if not bounced else -direction
    search_state["steps"] = int(search_state.get("steps", 0)) + 1
    return result["state"], True


def _run_target_center_follow_controller(args: argparse.Namespace) -> dict[str, Any]:
    sdk_mod = _load_local_controller_module()
    client = TargetPointClient(args.point_endpoint, timeout_sec=args.http_timeout_sec)
    if bool(args.require_status_check):
        status = client.get_status()
        if status.get("running") is False or status.get("ok") is False:
            raise RuntimeError(f"Target service status check failed: {status}")

    allowed_joints = set(str(name) for name in getattr(sdk_mod, "JOINTS", []))
    args.pan_joint = _normalize_optional_joint(args.pan_joint, allowed_joints)
    args.tilt_joint = _normalize_optional_joint(args.tilt_joint, allowed_joints)
    args.tilt_secondary_joint = _normalize_optional_joint(args.tilt_secondary_joint, allowed_joints)

    active_joints = [joint for joint in [args.pan_joint, args.tilt_joint, args.tilt_secondary_joint] if joint is not None]
    if len(active_joints) != len(set(active_joints)):
        raise ValidationError("pan/tilt joints must be different")
    if float(args.poll_interval_sec) < 0.0:
        raise ValidationError("--poll-interval-sec must be >= 0")
    if float(args.move_duration_sec) <= 0.0:
        raise ValidationError("--move-duration-sec must be > 0")
    if args.command_interval_sec is not None and float(args.command_interval_sec) < 0.0:
        raise ValidationError("--command-interval-sec must be >= 0")

    arm = sdk_mod.SoArmMoceController()
    start_state = arm.get_state()
    current_joint_state = dict(start_state["joint_state"])

    pan_axis = _build_controller_axis(
        joint_name=args.pan_joint,
        metric_key="ndx",
        gain_deg_per_norm=args.pan_gain_deg_per_norm,
        max_step_deg=args.pan_max_step_deg,
        dead_zone_norm=args.dead_zone_ndx,
        current_joint_deg=float(current_joint_state[args.pan_joint]) if args.pan_joint else 0.0,
        range_deg=args.pan_range_deg,
    )
    tilt_axis = _build_controller_axis(
        joint_name=args.tilt_joint,
        metric_key="ndy",
        gain_deg_per_norm=args.tilt_gain_deg_per_norm,
        max_step_deg=args.tilt_max_step_deg,
        dead_zone_norm=args.dead_zone_ndy,
        current_joint_deg=float(current_joint_state[args.tilt_joint]) if args.tilt_joint else 0.0,
        range_deg=args.tilt_range_deg,
    )
    tilt_secondary_axis = _build_controller_axis(
        joint_name=args.tilt_secondary_joint,
        metric_key="ndy",
        gain_deg_per_norm=args.tilt_secondary_gain_deg_per_norm,
        max_step_deg=args.tilt_secondary_max_step_deg,
        dead_zone_norm=args.dead_zone_ndy,
        current_joint_deg=float(current_joint_state[args.tilt_secondary_joint]) if args.tilt_secondary_joint else 0.0,
        range_deg=args.tilt_secondary_range_deg,
    )

    if pan_axis is None and tilt_axis is None and tilt_secondary_axis is None:
        raise ValidationError("At least one of pan/tilt control axes must be enabled")

    sign_cache_path = CONTROLLER_SIGN_CACHE_PATH
    sign_cache = {} if args.reprobe_control_signs else _load_sign_cache(sign_cache_path)
    command_interval_sec = (
        float(args.command_interval_sec)
        if args.command_interval_sec is not None
        else max(float(args.move_duration_sec), float(args.poll_interval_sec))
    )
    calibration: list[dict[str, Any]] = []
    iterations = 0
    misses = 0
    commands_sent = 0
    search_steps = 0
    no_target_streak = 0
    last_payload: dict[str, Any] | None = None
    interrupted = False

    pan_ready = _apply_joint_sign(
        axis=pan_axis,
        axis_key="pan",
        explicit_sign=args.pan_control_sign,
        use_cache=not args.reprobe_control_signs,
        sign_cache=sign_cache,
        calibration=calibration,
    )
    tilt_ready = _apply_joint_sign(
        axis=tilt_axis,
        axis_key="tilt_primary",
        explicit_sign=args.tilt_control_sign,
        use_cache=not args.reprobe_control_signs,
        sign_cache=sign_cache,
        calibration=calibration,
    )
    tilt_secondary_ready = _apply_joint_sign(
        axis=tilt_secondary_axis,
        axis_key="tilt_secondary",
        explicit_sign=args.tilt_secondary_control_sign,
        use_cache=not args.reprobe_control_signs,
        sign_cache=sign_cache,
        calibration=calibration,
    )

    next_motion_at = 0.0
    search_state = {
        "direction": 1.0,
        "step_deg": float(args.search_pan_step_deg),
        "min_deg": float(pan_axis.min_deg if pan_axis is not None else 0.0),
        "max_deg": float(pan_axis.max_deg if pan_axis is not None else 0.0),
        "steps": 0,
    }
    _log("robot transport=controller")
    _log(
        "runtime motion mode="
        + ("blocking" if args.wait_for_motion else f"non-blocking interval={command_interval_sec:.3f}s")
    )

    try:
        try:
            while True:
                payload = client.get_latest()
                last_payload = payload
                now_monotonic = time.monotonic()

                try:
                    observation = extract_target_observation(payload)
                    age_sec = max(0.0, time.time() - float(observation.timestamp))
                    if age_sec > float(args.max_point_staleness_sec):
                        raise TargetPayloadError(f"stale target payload: age={age_sec:.2f}s")
                except TargetPayloadError as exc:
                    misses += 1
                    no_target_streak += 1
                    _log(f"skip target frame: {exc}")
                    if no_target_streak >= int(args.search_miss_threshold) and (
                        args.wait_for_motion or now_monotonic >= next_motion_at
                    ):
                        current_state = arm.get_state()
                        current_joint_state = dict(current_state["joint_state"])
                        search_result, moved = _apply_controller_search_step(
                            arm,
                            current_joint_state=current_joint_state,
                            pan_axis=pan_axis,
                            search_state=search_state,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=bool(args.wait_for_motion),
                        )
                        if moved:
                            if args.wait_for_motion and search_result is not None:
                                current_joint_state = dict(search_result["joint_state"])
                            if not args.wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue

                no_target_streak = 0

                if pan_axis is not None and not pan_ready:
                    try:
                        probe_report = _probe_controller_axis_sign(
                            arm,
                            client,
                            pan_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        sign_cache["pan"] = float(pan_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        pan_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"pan axis ({pan_axis.joint_name})",
                            axis_optional=False,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            pan_axis = None
                            pan_ready = True

                if tilt_axis is not None and not tilt_ready:
                    try:
                        probe_report = _probe_controller_axis_sign(
                            arm,
                            client,
                            tilt_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        sign_cache["tilt_primary"] = float(tilt_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        tilt_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"tilt axis ({tilt_axis.joint_name})",
                            axis_optional=False,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            tilt_axis = None
                            tilt_ready = True

                if tilt_secondary_axis is not None and not tilt_secondary_ready:
                    try:
                        probe_report = _probe_controller_axis_sign(
                            arm,
                            client,
                            tilt_secondary_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        sign_cache["tilt_secondary"] = float(tilt_secondary_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        tilt_secondary_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"secondary tilt axis ({tilt_secondary_axis.joint_name})",
                            axis_optional=True,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            tilt_secondary_axis = None
                            tilt_secondary_ready = True

                if pan_axis is None and tilt_axis is None and tilt_secondary_axis is None:
                    raise RuntimeError("all control axes are disabled")

                now_monotonic = time.monotonic()
                if not args.wait_for_motion and now_monotonic < next_motion_at:
                    time.sleep(args.poll_interval_sec)
                    continue

                current_state = arm.get_state()
                current_joint_state = dict(current_state["joint_state"])
                targets: dict[str, float] = {}

                if pan_axis is not None:
                    next_target = pan_axis.compute_next_target(float(current_joint_state[pan_axis.joint_name]), observation.ndx)
                    if next_target is not None and abs(next_target - float(current_joint_state[pan_axis.joint_name])) >= args.min_command_deg:
                        targets[pan_axis.joint_name] = next_target

                if tilt_axis is not None:
                    next_target = tilt_axis.compute_next_target(float(current_joint_state[tilt_axis.joint_name]), observation.ndy)
                    if next_target is not None and abs(next_target - float(current_joint_state[tilt_axis.joint_name])) >= args.min_command_deg:
                        targets[tilt_axis.joint_name] = next_target

                if tilt_secondary_axis is not None:
                    next_target = tilt_secondary_axis.compute_next_target(
                        float(current_joint_state[tilt_secondary_axis.joint_name]),
                        observation.ndy,
                    )
                    if (
                        next_target is not None
                        and abs(next_target - float(current_joint_state[tilt_secondary_axis.joint_name])) >= args.min_command_deg
                    ):
                        targets[tilt_secondary_axis.joint_name] = next_target

                iterations += 1
                if not targets:
                    _log(
                        f"hold iter={iterations} frame={observation.frame_id} "
                        f"ndx={observation.ndx:+.4f} ndy={observation.ndy:+.4f}"
                    )
                    time.sleep(args.poll_interval_sec)
                    continue

                if args.wait_for_motion:
                    result = arm.move_joints(
                        targets_deg=targets,
                        duration=args.move_duration_sec,
                        wait=True,
                    )
                    current_joint_state = dict(result["state"]["joint_state"])
                else:
                    arm.move_joints(
                        targets_deg=targets,
                        duration=args.move_duration_sec,
                        wait=False,
                    )
                    next_motion_at = time.monotonic() + command_interval_sec
                commands_sent += 1
                _log(
                    f"move iter={iterations} frame={observation.frame_id} "
                    f"ndx={observation.ndx:+.4f} ndy={observation.ndy:+.4f} "
                    f"targets_deg={json.dumps(targets, ensure_ascii=False, sort_keys=True)}"
                )
        except KeyboardInterrupt:
            interrupted = True
            _log("received Ctrl+C; stopping target center follow loop")

        if bool(args.hold_on_exit):
            hold_result = arm.stop()
            current_joint_state = dict(hold_result["state"]["joint_state"])

        return {
            "action": "target_center_follow",
            "point_endpoint": client.latest_url,
            "status_endpoint": client.status_url,
            "robot_transport": "controller",
            "sign_cache_path": str(sign_cache_path),
            "calibration": calibration,
            "start_joint_state_deg": {name: float(value) for name, value in start_state["joint_state"].items()},
            "final_joint_state_deg": {name: float(value) for name, value in current_joint_state.items()},
            "iterations": iterations,
            "commands_sent": commands_sent,
            "search_steps": search_steps,
            "misses": misses,
            "stopped_by_user": interrupted,
            "last_target_payload": last_payload,
            "wait_for_motion": bool(args.wait_for_motion),
            "command_interval_sec": command_interval_sec,
        }
    finally:
        arm.close()


def _run_target_center_follow_robot_sdk(args: argparse.Namespace) -> dict[str, Any]:
    client = TargetPointClient(args.point_endpoint, timeout_sec=args.http_timeout_sec)
    if bool(args.require_status_check):
        status = client.get_status()
        if status.get("running") is False or status.get("ok") is False:
            raise RuntimeError(f"Target service status check failed: {status}")

    robot = Robot.from_config(args.config) if args.config else Robot()
    robot.connect()
    try:
        allowed_joints = set(robot.robot_model.joint_name_to_index.keys())
        args.pan_joint = _normalize_optional_joint(args.pan_joint, allowed_joints)
        args.tilt_joint = _normalize_optional_joint(args.tilt_joint, allowed_joints)
        args.tilt_secondary_joint = _normalize_optional_joint(args.tilt_secondary_joint, allowed_joints)

        active_joints = [joint for joint in [args.pan_joint, args.tilt_joint, args.tilt_secondary_joint] if joint is not None]
        if len(active_joints) != len(set(active_joints)):
            raise ValidationError("pan/tilt joints must be different")
        if float(args.poll_interval_sec) < 0.0:
            raise ValidationError("--poll-interval-sec must be >= 0")
        if float(args.move_duration_sec) <= 0.0:
            raise ValidationError("--move-duration-sec must be > 0")
        if args.command_interval_sec is not None and float(args.command_interval_sec) < 0.0:
            raise ValidationError("--command-interval-sec must be >= 0")

        start_state = robot.get_state()
        start_q = np.asarray(start_state.joint_state.q, dtype=float).copy()
        current_q = start_q.copy()
        robot_source = str(start_state.actual.source if start_state.actual is not None else "unknown")
        _log(f"robot transport={robot_source}")
        if robot_source == "mock":
            _warn("robot is using mock transport; this run will not move the real arm")

        pan_axis = _build_axis(
            robot=robot,
            joint_name=args.pan_joint,
            metric_key="ndx",
            gain_deg_per_norm=args.pan_gain_deg_per_norm,
            max_step_deg=args.pan_max_step_deg,
            dead_zone_norm=args.dead_zone_ndx,
            current_joint_rad=float(current_q[robot.robot_model.resolve_joint_index(args.pan_joint)]) if args.pan_joint else 0.0,
            range_deg=args.pan_range_deg,
        )
        tilt_axis = _build_axis(
            robot=robot,
            joint_name=args.tilt_joint,
            metric_key="ndy",
            gain_deg_per_norm=args.tilt_gain_deg_per_norm,
            max_step_deg=args.tilt_max_step_deg,
            dead_zone_norm=args.dead_zone_ndy,
            current_joint_rad=float(current_q[robot.robot_model.resolve_joint_index(args.tilt_joint)]) if args.tilt_joint else 0.0,
            range_deg=args.tilt_range_deg,
        )
        tilt_secondary_axis = _build_axis(
            robot=robot,
            joint_name=args.tilt_secondary_joint,
            metric_key="ndy",
            gain_deg_per_norm=args.tilt_secondary_gain_deg_per_norm,
            max_step_deg=args.tilt_secondary_max_step_deg,
            dead_zone_norm=args.dead_zone_ndy,
            current_joint_rad=(
                float(current_q[robot.robot_model.resolve_joint_index(args.tilt_secondary_joint)])
                if args.tilt_secondary_joint
                else 0.0
            ),
            range_deg=args.tilt_secondary_range_deg,
        )

        if pan_axis is None and tilt_axis is None and tilt_secondary_axis is None:
            raise ValidationError("At least one of pan/tilt control axes must be enabled")

        sign_cache_path = SIGN_CACHE_PATH
        sign_cache = {} if args.reprobe_control_signs else _load_sign_cache(sign_cache_path)
        command_interval_sec = (
            float(args.command_interval_sec)
            if args.command_interval_sec is not None
            else max(float(args.move_duration_sec), float(args.poll_interval_sec))
        )
        probe_delta_rad = _deg_to_rad(args.probe_delta_deg)
        min_command_rad = _deg_to_rad(args.min_command_deg)
        calibration: list[dict[str, Any]] = []
        iterations = 0
        misses = 0
        commands_sent = 0
        search_steps = 0
        no_target_streak = 0
        last_payload: dict[str, Any] | None = None
        interrupted = False

        pan_ready = _apply_joint_sign(
            axis=pan_axis,
            axis_key="pan",
            explicit_sign=args.pan_control_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        tilt_ready = _apply_joint_sign(
            axis=tilt_axis,
            axis_key="tilt_primary",
            explicit_sign=args.tilt_control_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        tilt_secondary_ready = _apply_joint_sign(
            axis=tilt_secondary_axis,
            axis_key="tilt_secondary",
            explicit_sign=args.tilt_secondary_control_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )

        next_motion_at = 0.0
        search_state = {
            "direction": 1.0,
            "step_deg": float(args.search_pan_step_deg),
            "min_rad": float(pan_axis.min_rad if pan_axis is not None else 0.0),
            "max_rad": float(pan_axis.max_rad if pan_axis is not None else 0.0),
            "steps": 0,
        }

        _log(
            "runtime motion mode="
            + ("blocking" if args.wait_for_motion else f"non-blocking interval={command_interval_sec:.3f}s")
        )

        try:
            while True:
                payload = client.get_latest()
                last_payload = payload
                now_monotonic = time.monotonic()

                try:
                    observation = extract_target_observation(payload)
                    age_sec = max(0.0, time.time() - float(observation.timestamp))
                    if age_sec > float(args.max_point_staleness_sec):
                        raise TargetPayloadError(f"stale target payload: age={age_sec:.2f}s")
                except TargetPayloadError as exc:
                    misses += 1
                    no_target_streak += 1
                    _log(f"skip target frame: {exc}")
                    if no_target_streak >= int(args.search_miss_threshold) and (
                        args.wait_for_motion or now_monotonic >= next_motion_at
                    ):
                        current_q = robot.get_joint_state().q.copy()
                        search_result, moved = _apply_search_step(
                            robot,
                            current_q=current_q,
                            pan_axis=pan_axis,
                            search_state=search_state,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=bool(args.wait_for_motion),
                        )
                        if moved:
                            if search_result is not None:
                                current_q = search_result.copy()
                            if not args.wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue

                no_target_streak = 0

                if pan_axis is not None and not pan_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            robot,
                            client,
                            pan_axis,
                            probe_delta_rad=probe_delta_rad,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        sign_cache["pan"] = float(pan_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        pan_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"pan axis ({pan_axis.joint_name})",
                            axis_optional=False,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            pan_axis = None
                            pan_ready = True

                if tilt_axis is not None and not tilt_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            robot,
                            client,
                            tilt_axis,
                            probe_delta_rad=probe_delta_rad,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        sign_cache["tilt_primary"] = float(tilt_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        tilt_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"tilt axis ({tilt_axis.joint_name})",
                            axis_optional=False,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            tilt_axis = None
                            tilt_ready = True

                if tilt_secondary_axis is not None and not tilt_secondary_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            robot,
                            client,
                            tilt_secondary_axis,
                            probe_delta_rad=probe_delta_rad,
                            move_duration_sec=args.move_duration_sec,
                            point_timeout_sec=args.point_timeout_sec,
                            max_point_staleness_sec=args.max_point_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        sign_cache["tilt_secondary"] = float(tilt_secondary_axis.control_sign)
                        _save_sign_cache(sign_cache, sign_cache_path)
                        tilt_secondary_ready = True
                    except Exception as exc:
                        if _handle_probe_failure(
                            axis_label=f"secondary tilt axis ({tilt_secondary_axis.joint_name})",
                            axis_optional=True,
                            policy=args.probe_failure_policy,
                            exc=exc,
                        ):
                            tilt_secondary_axis = None
                            tilt_secondary_ready = True

                if pan_axis is None and tilt_axis is None and tilt_secondary_axis is None:
                    raise RuntimeError("all control axes are disabled")

                now_monotonic = time.monotonic()
                if not args.wait_for_motion and now_monotonic < next_motion_at:
                    time.sleep(args.poll_interval_sec)
                    continue

                current_q = robot.get_joint_state().q.copy()
                q_target = current_q.copy()
                changed = False

                if pan_axis is not None:
                    next_target = pan_axis.compute_next_target(float(current_q[pan_axis.joint_index]), observation.ndx)
                    if next_target is not None and abs(next_target - float(current_q[pan_axis.joint_index])) >= min_command_rad:
                        q_target[pan_axis.joint_index] = next_target
                        changed = True

                if tilt_axis is not None:
                    next_target = tilt_axis.compute_next_target(float(current_q[tilt_axis.joint_index]), observation.ndy)
                    if next_target is not None and abs(next_target - float(current_q[tilt_axis.joint_index])) >= min_command_rad:
                        q_target[tilt_axis.joint_index] = next_target
                        changed = True

                if tilt_secondary_axis is not None:
                    next_target = tilt_secondary_axis.compute_next_target(
                        float(current_q[tilt_secondary_axis.joint_index]),
                        observation.ndy,
                    )
                    if (
                        next_target is not None
                        and abs(next_target - float(current_q[tilt_secondary_axis.joint_index])) >= min_command_rad
                    ):
                        q_target[tilt_secondary_axis.joint_index] = next_target
                        changed = True

                iterations += 1
                if not changed:
                    _log(
                        f"hold iter={iterations} frame={observation.frame_id} "
                        f"ndx={observation.ndx:+.4f} ndy={observation.ndy:+.4f}"
                    )
                    time.sleep(args.poll_interval_sec)
                    continue

                previous_q = current_q.copy()
                _move_q(robot, q_target, duration_sec=args.move_duration_sec, wait=bool(args.wait_for_motion))
                current_q = q_target.copy()
                commands_sent += 1
                if not args.wait_for_motion:
                    next_motion_at = time.monotonic() + command_interval_sec

                move_payload = {
                    name: round(_rad_to_deg(q_target[idx]), 3)
                    for idx, name in enumerate(robot.robot_model.joint_names)
                    if abs(float(q_target[idx] - previous_q[idx])) >= min_command_rad
                }
                _log(
                    f"move iter={iterations} frame={observation.frame_id} "
                    f"ndx={observation.ndx:+.4f} ndy={observation.ndy:+.4f} "
                    f"targets_deg={json.dumps(move_payload, ensure_ascii=False, sort_keys=True)}"
                )
        except KeyboardInterrupt:
            interrupted = True
            _log("received Ctrl+C; stopping target center follow loop")

        if bool(args.hold_on_exit):
            robot.stop()

        final_q = robot.get_joint_state().q.copy()
        return {
            "action": "target_center_follow",
            "point_endpoint": client.latest_url,
            "status_endpoint": client.status_url,
            "robot_transport": robot_source,
            "sign_cache_path": str(sign_cache_path),
            "calibration": calibration,
            "start_joint_state_deg": _joint_state_deg_payload(robot, start_q),
            "final_joint_state_deg": _joint_state_deg_payload(robot, final_q),
            "iterations": iterations,
            "commands_sent": commands_sent,
            "search_steps": search_steps,
            "misses": misses,
            "stopped_by_user": interrupted,
            "last_target_payload": last_payload,
            "wait_for_motion": bool(args.wait_for_motion),
            "command_interval_sec": command_interval_sec,
            "axes": {
                "pan": None
                if pan_axis is None
                else {
                    "joint": pan_axis.joint_name,
                    "metric": pan_axis.metric_key,
                    "control_sign": pan_axis.control_sign,
                    "dead_zone_norm": pan_axis.dead_zone_norm,
                    "gain_deg_per_norm": round(_rad_to_deg(pan_axis.gain_rad_per_norm), 6),
                    "max_step_deg": round(_rad_to_deg(pan_axis.max_step_rad), 6),
                    "min_deg": round(_rad_to_deg(pan_axis.min_rad), 6),
                    "max_deg": round(_rad_to_deg(pan_axis.max_rad), 6),
                },
                "tilt": None
                if tilt_axis is None
                else {
                    "joint": tilt_axis.joint_name,
                    "metric": tilt_axis.metric_key,
                    "control_sign": tilt_axis.control_sign,
                    "dead_zone_norm": tilt_axis.dead_zone_norm,
                    "gain_deg_per_norm": round(_rad_to_deg(tilt_axis.gain_rad_per_norm), 6),
                    "max_step_deg": round(_rad_to_deg(tilt_axis.max_step_rad), 6),
                    "min_deg": round(_rad_to_deg(tilt_axis.min_rad), 6),
                    "max_deg": round(_rad_to_deg(tilt_axis.max_rad), 6),
                },
                "tilt_secondary": None
                if tilt_secondary_axis is None
                else {
                    "joint": tilt_secondary_axis.joint_name,
                    "metric": tilt_secondary_axis.metric_key,
                    "control_sign": tilt_secondary_axis.control_sign,
                    "dead_zone_norm": tilt_secondary_axis.dead_zone_norm,
                    "gain_deg_per_norm": round(_rad_to_deg(tilt_secondary_axis.gain_rad_per_norm), 6),
                    "max_step_deg": round(_rad_to_deg(tilt_secondary_axis.max_step_rad), 6),
                    "min_deg": round(_rad_to_deg(tilt_secondary_axis.min_rad), 6),
                    "max_deg": round(_rad_to_deg(tilt_secondary_axis.max_rad), 6),
                },
            },
        }
    finally:
        try:
            robot.disconnect()
        except Exception:
            pass


def run_target_center_follow(args: argparse.Namespace) -> dict[str, Any]:
    backend = str(getattr(args, "backend", "controller") or "controller").strip().lower()
    if backend in {"controller", "local-controller", "real"}:
        return _run_target_center_follow_controller(args)
    if backend in {"robot-sdk", "sdk", "mock"}:
        return _run_target_center_follow_robot_sdk(args)
    raise ValidationError("--backend must be one of: controller, robot-sdk")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep a target center near the frame center with soarmMoce SDK"
    )
    parser.add_argument("--backend", default="controller", help="controller | robot-sdk")
    parser.add_argument("--point-endpoint", default="http://127.0.0.1:8012", help="Target service base URL or /latest URL")
    parser.add_argument("--config", default=None, help="Optional SDK config path")
    parser.add_argument("--require-status-check", action="store_true", help="Require GET /status to succeed before motion starts")
    parser.add_argument("--http-timeout-sec", type=float, default=1.5)
    parser.add_argument("--poll-interval-sec", type=float, default=0.02)
    parser.add_argument("--move-duration-sec", type=float, default=0.12)
    parser.add_argument("--point-timeout-sec", type=float, default=3.0, help="Wait timeout when probing control sign")
    parser.add_argument("--max-point-staleness-sec", type=float, default=1.5)
    parser.add_argument("--min-probe-metric-delta", type=float, default=0.005)
    parser.add_argument("--probe-delta-deg", type=float, default=1.5)
    parser.add_argument("--probe-failure-policy", default="skip-optional", help="strict | disable-axis | skip-optional")
    parser.add_argument("--reprobe-control-signs", action="store_true", help="Ignore cached signs and probe again")
    parser.add_argument("--pan-control-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--tilt-control-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--tilt-secondary-control-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--pan-joint", default="shoulder_pan")
    parser.add_argument("--tilt-joint", default="shoulder_lift", help="Primary vertical joint or 'none' to disable")
    parser.add_argument("--tilt-secondary-joint", default="elbow_flex", help="Secondary vertical joint or 'none' to disable")
    parser.add_argument("--pan-range-deg", type=float, default=40.0, help="Allowed pan motion around startup pose")
    parser.add_argument("--tilt-range-deg", type=float, default=18.0, help="Allowed primary vertical motion around startup pose")
    parser.add_argument("--tilt-secondary-range-deg", type=float, default=18.0, help="Allowed secondary vertical motion around startup pose")
    parser.add_argument("--pan-gain-deg-per-norm", type=float, default=10.0)
    parser.add_argument("--tilt-gain-deg-per-norm", type=float, default=4.5)
    parser.add_argument("--tilt-secondary-gain-deg-per-norm", type=float, default=3.5)
    parser.add_argument("--pan-max-step-deg", type=float, default=2.4)
    parser.add_argument("--tilt-max-step-deg", type=float, default=1.2)
    parser.add_argument("--tilt-secondary-max-step-deg", type=float, default=1.0)
    parser.add_argument("--dead-zone-ndx", type=float, default=0.06)
    parser.add_argument("--dead-zone-ndy", type=float, default=0.12)
    parser.add_argument("--min-command-deg", type=float, default=0.12)
    parser.add_argument("--search-miss-threshold", type=int, default=1)
    parser.add_argument("--search-pan-step-deg", type=float, default=1.6)
    parser.add_argument(
        "--wait-for-motion",
        type=lambda raw: str(raw).strip().lower() in {"1", "true", "yes", "on"},
        default=False,
        help="Wait for each commanded move to finish before processing the next control step",
    )
    parser.add_argument(
        "--command-interval-sec",
        type=float,
        default=None,
        help="Minimum spacing between runtime motion commands when wait-for-motion is false; defaults to move duration",
    )
    parser.add_argument(
        "--hold-on-exit",
        type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"},
        default=True,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.pan_control_sign = _normalize_sign_arg(args.pan_control_sign, "--pan-control-sign")
        args.tilt_control_sign = _normalize_sign_arg(args.tilt_control_sign, "--tilt-control-sign")
        args.tilt_secondary_control_sign = _normalize_sign_arg(
            args.tilt_secondary_control_sign,
            "--tilt-secondary-control-sign",
        )
        args.probe_failure_policy = _normalize_probe_policy(args.probe_failure_policy)
        _emit(run_target_center_follow(args))
    except KeyboardInterrupt:
        _emit({"ok": False, "error": "interrupted"}, exit_code=130)
    except Exception as exc:
        _emit({"ok": False, "error": str(exc), "type": exc.__class__.__name__}, exit_code=1)


if __name__ == "__main__":
    main()
