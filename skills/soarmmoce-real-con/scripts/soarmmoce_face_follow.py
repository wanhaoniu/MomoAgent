#!/usr/bin/env python3
"""Keep a detected face near the camera center using the real soarmMoce arm."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import JOINTS, SoArmMoceController, ValidationError


SIGN_CACHE_PATH = Path(__file__).resolve().parents[1] / "calibration" / "face_follow_signs.json"


def _log(message: str) -> None:
    print(f"[face-follow] {message}", file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    print(f"[face-follow][warn] {message}", file=sys.stderr, flush=True)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _normalize_optional_joint(raw: str | None) -> Optional[str]:
    value = str(raw or "").strip().lower()
    if value in {"", "none", "off", "disable", "disabled"}:
        return None
    if value not in JOINTS:
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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def _apply_cartesian_sign(
    *,
    axis: CartesianAxis,
    axis_key: str,
    explicit_sign: Optional[float],
    use_cache: bool,
    sign_cache: dict[str, float],
    calibration: list[dict[str, Any]],
) -> bool:
    if explicit_sign is not None:
        axis.effect_sign = float(explicit_sign)
        calibration.append(
            {
                "axis": axis.name,
                "metric": axis.metric_key,
                "effect_sign": axis.effect_sign,
                "mode": "manual",
                "cache_key": axis_key,
            }
        )
        _log(f"axis sign fixed: axis={axis.name} effect_sign={axis.effect_sign:+.1f}")
        return True
    if use_cache and axis_key in sign_cache:
        axis.effect_sign = float(sign_cache[axis_key])
        calibration.append(
            {
                "axis": axis.name,
                "metric": axis.metric_key,
                "effect_sign": axis.effect_sign,
                "mode": "cache",
                "cache_key": axis_key,
            }
        )
        _log(f"axis sign cached: axis={axis.name} effect_sign={axis.effect_sign:+.1f}")
        return True
    return False


def _fetch_json(url: str, timeout_sec: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_sec) as response:
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


class FaceTrackingClient:
    def __init__(self, endpoint: str, timeout_sec: float) -> None:
        base = str(endpoint).strip().rstrip("/")
        if not base:
            raise ValidationError("--face-endpoint is required")
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


@dataclass(slots=True)
class CartesianAxis:
    name: str
    metric_key: str
    component: str
    gain_per_metric: float
    max_step: float
    min_value: float
    max_value: float
    effect_sign: float = 0.0

    def current_value(self, state: dict[str, Any]) -> float:
        xyz = state["tcp_pose"]["xyz"]
        idx = {"x": 0, "y": 1, "z": 2}[self.component]
        return float(xyz[idx])

    def compute_step(self, metric_value: float, *, target_value: float = 0.0) -> float:
        raw_step = self.effect_sign * self.gain_per_metric * (float(target_value) - float(metric_value))
        return _clamp(raw_step, -self.max_step, self.max_step)


def _extract_face_metric(payload: dict[str, Any], metric_key: str) -> float:
    if not bool(payload.get("detected")):
        raise RuntimeError("No face detected in latest payload")
    if metric_key == "area_ratio":
        smoothed_face = payload.get("smoothed_target_face") or {}
        if "area_ratio" in smoothed_face:
            return float(smoothed_face["area_ratio"])
        target_face = payload.get("target_face") or {}
        if "area_ratio" in target_face:
            return float(target_face["area_ratio"])
        raise RuntimeError("Face payload is missing area_ratio")
    # Joint direction probing needs the immediate image response.
    # Using the raw offset first avoids EMA lag flipping the inferred sign.
    offset = payload.get("offset") or payload.get("smoothed_offset") or {}
    if metric_key not in offset:
        raise RuntimeError(f"Face payload is missing offset metric: {metric_key}")
    return float(offset[metric_key])


def _wait_for_face(
    client: FaceTrackingClient,
    *,
    timeout_sec: float,
    max_staleness_sec: float,
    newer_than_frame_id: Optional[int] = None,
) -> dict[str, Any]:
    deadline = time.time() + max(0.1, float(timeout_sec))
    last_problem = "face tracking service did not return a usable face payload"
    while time.time() < deadline:
        payload = client.get_latest()
        timestamp = float(payload.get("timestamp") or 0.0)
        age_sec = max(0.0, time.time() - timestamp)
        frame_id = int(payload.get("frame_id") or 0)
        if age_sec > max_staleness_sec:
            last_problem = f"stale face payload: age={age_sec:.2f}s"
            time.sleep(0.05)
            continue
        if newer_than_frame_id is not None and frame_id <= newer_than_frame_id:
            last_problem = f"waiting for a newer frame than {newer_than_frame_id}"
            time.sleep(0.05)
            continue
        if payload.get("status") != "tracking" or not bool(payload.get("detected")):
            last_problem = f"tracking status={payload.get('status')} detected={payload.get('detected')}"
            time.sleep(0.05)
            continue
        return payload
    raise RuntimeError(last_problem)


def _collect_metric_median(
    client: FaceTrackingClient,
    *,
    metric_key: str,
    sample_count: int,
    timeout_sec: float,
    max_staleness_sec: float,
    newer_than_frame_id: Optional[int] = None,
) -> tuple[float, dict[str, Any]]:
    samples: list[float] = []
    latest_payload: dict[str, Any] | None = None
    last_frame_id = newer_than_frame_id

    for _ in range(max(1, int(sample_count))):
        payload = _wait_for_face(
            client,
            timeout_sec=timeout_sec,
            max_staleness_sec=max_staleness_sec,
            newer_than_frame_id=last_frame_id,
        )
        last_frame_id = int(payload.get("frame_id") or 0)
        latest_payload = payload
        samples.append(_extract_face_metric(payload, metric_key))

    return float(statistics.median(samples)), latest_payload or {}


def _probe_axis_sign(
    arm: SoArmMoceController,
    client: FaceTrackingClient,
    axis: JointAxis,
    *,
    probe_delta_deg: float,
    move_duration_sec: float,
    face_timeout_sec: float,
    max_face_staleness_sec: float,
    min_probe_metric_delta: float,
) -> dict[str, Any]:
    baseline_metric, baseline = _collect_metric_median(
        client,
        metric_key=axis.metric_key,
        sample_count=3,
        timeout_sec=face_timeout_sec,
        max_staleness_sec=max_face_staleness_sec,
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
        moved: dict[str, Any] | None = None
        revert: dict[str, Any] | None = None
        try:
            moved_metric, moved = _collect_metric_median(
                client,
                metric_key=axis.metric_key,
                sample_count=3,
                timeout_sec=face_timeout_sec,
                max_staleness_sec=max_face_staleness_sec,
                newer_than_frame_id=int(baseline.get("frame_id") or 0),
            )
        finally:
            revert = arm.move_joint(
                joint=axis.joint_name,
                delta_deg=-effective_probe_delta,
                duration=move_duration_sec,
                wait=True,
            )
            newer_than = int(moved.get("frame_id") or 0) if moved is not None else int(baseline.get("frame_id") or 0)
            try:
                _wait_for_face(
                    client,
                    timeout_sec=face_timeout_sec,
                    max_staleness_sec=max_face_staleness_sec,
                    newer_than_frame_id=newer_than,
                )
            except Exception:
                pass

        metric_delta = moved_metric - baseline_metric
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
        f"even after probing up to {last_result['probe_delta_deg']:.3f}deg; effect is too small to determine control direction"
    )


def _probe_cartesian_axis_sign(
    arm: SoArmMoceController,
    client: FaceTrackingClient,
    axis: CartesianAxis,
    *,
    probe_step: float,
    move_duration_sec: float,
    face_timeout_sec: float,
    max_face_staleness_sec: float,
    min_probe_metric_delta: float,
) -> dict[str, Any]:
    baseline_metric, baseline = _collect_metric_median(
        client,
        metric_key=axis.metric_key,
        sample_count=3,
        timeout_sec=face_timeout_sec,
        max_staleness_sec=max_face_staleness_sec,
    )

    probe_multipliers = [1.0, 1.75, 2.5]
    last_result: dict[str, Any] | None = None
    for multiplier in probe_multipliers:
        effective_probe = float(probe_step) * float(multiplier)
        delta_kwargs = {"dx": 0.0, "dy": 0.0, "dz": 0.0}
        delta_kwargs[axis.component] = effective_probe
        _log(
            f"probing {axis.name} on {axis.metric_key}: baseline={baseline_metric:+.4f}, "
            f"{axis.component}+={effective_probe:.4f}m"
        )
        move_plus = arm.move_delta(
            dx=delta_kwargs["dx"],
            dy=delta_kwargs["dy"],
            dz=delta_kwargs["dz"],
            frame="urdf",
            duration=move_duration_sec,
            wait=True,
        )
        moved: dict[str, Any] | None = None
        revert: dict[str, Any] | None = None
        try:
            moved_metric, moved = _collect_metric_median(
                client,
                metric_key=axis.metric_key,
                sample_count=3,
                timeout_sec=face_timeout_sec,
                max_staleness_sec=max_face_staleness_sec,
                newer_than_frame_id=int(baseline.get("frame_id") or 0),
            )
        finally:
            revert = arm.move_delta(
                dx=-delta_kwargs["dx"],
                dy=-delta_kwargs["dy"],
                dz=-delta_kwargs["dz"],
                frame="urdf",
                duration=move_duration_sec,
                wait=True,
            )
            newer_than = int(moved.get("frame_id") or 0) if moved is not None else int(baseline.get("frame_id") or 0)
            try:
                _wait_for_face(
                    client,
                    timeout_sec=face_timeout_sec,
                    max_staleness_sec=max_face_staleness_sec,
                    newer_than_frame_id=newer_than,
                )
            except Exception:
                pass

        metric_delta = moved_metric - baseline_metric
        effect_sign = 1.0 if metric_delta > 0.0 else -1.0
        last_result = {
            "axis": axis.name,
            "metric": axis.metric_key,
            "probe_step": float(effective_probe),
            "component": axis.component,
            "baseline_metric": float(baseline_metric),
            "moved_metric": float(moved_metric),
            "metric_delta": float(metric_delta),
            "effect_sign": float(effect_sign),
            "revert_state": revert["state"] if revert is not None else {},
            "move_state": move_plus["state"],
        }
        if abs(metric_delta) >= float(min_probe_metric_delta):
            axis.effect_sign = effect_sign
            return last_result
        _warn(
            f"probe too weak on {axis.name}: delta={metric_delta:+.5f} with "
            f"{effective_probe:.4f}m; trying a larger probe"
        )

    if last_result is None:
        raise RuntimeError(f"Probe on {axis.name} did not produce any usable measurement")
    raise RuntimeError(
        f"Probe on {axis.name} changed {axis.metric_key} by only {last_result['metric_delta']:+.5f} "
        f"even after probing up to {last_result['probe_step']:.4f}m; effect is too small to determine control direction"
    )


def _build_axis(
    *,
    joint_name: Optional[str],
    metric_key: str,
    gain_deg_per_norm: float,
    max_step_deg: float,
    dead_zone_norm: float,
    current_joint_deg: float,
    range_deg: float,
) -> Optional[JointAxis]:
    if joint_name is None:
        return None
    span = max(0.5, float(range_deg))
    return JointAxis(
        joint_name=joint_name,
        metric_key=metric_key,
        gain_deg_per_norm=float(gain_deg_per_norm),
        max_step_deg=max(0.1, float(max_step_deg)),
        dead_zone_norm=max(0.0, float(dead_zone_norm)),
        min_deg=float(current_joint_deg) - span,
        max_deg=float(current_joint_deg) + span,
    )


def _normalize_probe_policy(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "skip-optional"}:
        return "skip-optional"
    if value in {"strict", "disable-axis", "skip-optional"}:
        return value
    raise ValidationError("--probe-failure-policy must be one of: strict, disable-axis, skip-optional")


def _apply_search_step(
    arm: SoArmMoceController,
    *,
    current_joint_state: dict[str, float],
    pan_axis: JointAxis | None,
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


def _select_motion_mode(
    *,
    has_joint_targets: bool,
    has_cartesian_delta: bool,
    preferred_mode: str,
) -> tuple[str | None, str]:
    if has_joint_targets:
        return "joint", "joint"
    if has_cartesian_delta:
        return "cartesian", "joint"
    return None, "joint"


def run_face_follow(args: argparse.Namespace) -> dict[str, Any]:
    active_joints = [joint for joint in [args.pan_joint, args.tilt_joint, args.tilt_secondary_joint] if joint is not None]
    if len(active_joints) != len(set(active_joints)):
        raise ValidationError("pan/tilt joints must be different")
    if float(args.poll_interval_sec) < 0.0:
        raise ValidationError("--poll-interval-sec must be >= 0")
    if float(args.move_duration_sec) <= 0.0:
        raise ValidationError("--move-duration-sec must be > 0")
    if float(args.depth_area_dead_zone) < 0.0:
        raise ValidationError("--depth-area-dead-zone must be >= 0")
    if args.command_interval_sec is not None and float(args.command_interval_sec) < 0.0:
        raise ValidationError("--command-interval-sec must be >= 0")

    client = FaceTrackingClient(args.face_endpoint, timeout_sec=args.http_timeout_sec)
    status = client.get_status()
    if not bool(status.get("running", status.get("engine_running", False))):
        raise RuntimeError(f"Face tracking service is not running: {status}")

    arm = SoArmMoceController()
    start_state = arm.get_state()
    current_joint_state = dict(start_state["joint_state"])
    current_state = dict(start_state)
    start_tcp = {
        "x": float(start_state["tcp_pose"]["xyz"][0]),
        "y": float(start_state["tcp_pose"]["xyz"][1]),
        "z": float(start_state["tcp_pose"]["xyz"][2]),
    }

    pan_axis = _build_axis(
        joint_name=args.pan_joint,
        metric_key="ndx",
        gain_deg_per_norm=args.pan_gain_deg_per_norm,
        max_step_deg=args.pan_max_step_deg,
        dead_zone_norm=args.dead_zone_ndx,
        current_joint_deg=float(current_joint_state[args.pan_joint]) if args.pan_joint else 0.0,
        range_deg=args.pan_range_deg,
    )
    tilt_axis = _build_axis(
        joint_name=args.tilt_joint,
        metric_key="ndy",
        gain_deg_per_norm=args.tilt_gain_deg_per_norm,
        max_step_deg=args.tilt_max_step_deg,
        dead_zone_norm=args.dead_zone_ndy,
        current_joint_deg=float(current_joint_state[args.tilt_joint]) if args.tilt_joint else 0.0,
        range_deg=args.tilt_range_deg,
    )
    tilt_secondary_axis = _build_axis(
        joint_name=args.tilt_secondary_joint,
        metric_key="ndy",
        gain_deg_per_norm=args.tilt_secondary_gain_deg_per_norm,
        max_step_deg=args.tilt_secondary_max_step_deg,
        dead_zone_norm=args.dead_zone_ndy,
        current_joint_deg=float(current_joint_state[args.tilt_secondary_joint]) if args.tilt_secondary_joint else 0.0,
        range_deg=args.tilt_secondary_range_deg,
    )
    lift_axis = CartesianAxis(
        name="lift_z",
        metric_key="ndy",
        component="z",
        gain_per_metric=float(args.lift_gain_m_per_norm),
        max_step=float(args.lift_max_step_m),
        min_value=float(start_tcp["z"]) - float(args.lift_range_m),
        max_value=float(start_tcp["z"]) + float(args.lift_range_m),
    )
    depth_axis = CartesianAxis(
        name="depth_x",
        metric_key="area_ratio",
        component="x",
        gain_per_metric=float(args.depth_gain_m_per_area),
        max_step=float(args.depth_max_step_m),
        min_value=float(start_tcp["x"]) - float(args.depth_range_m),
        max_value=float(start_tcp["x"]) + float(args.depth_range_m),
    )
    depth_target_area_ratio = (
        float(args.depth_target_area_ratio)
        if args.depth_target_area_ratio is not None
        else 0.5 * (float(args.depth_min_area_ratio) + float(args.depth_max_area_ratio))
    )
    sign_cache = {} if args.reprobe_control_signs else _load_sign_cache()
    wait_for_motion = bool(args.wait_for_motion)
    command_interval_sec = (
        float(args.command_interval_sec)
        if args.command_interval_sec is not None
        else max(float(args.move_duration_sec), float(args.poll_interval_sec))
    )

    if pan_axis is None and tilt_axis is None and tilt_secondary_axis is None and not args.enable_lift and not args.enable_depth:
        raise ValidationError("At least one of pan/tilt/lift/depth control axes must be enabled")

    calibration: list[dict[str, Any]] = []
    try:
        iterations = 0
        misses = 0
        commands_sent = 0
        search_steps = 0
        no_face_streak = 0
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
        lift_ready = (not args.enable_lift) or _apply_cartesian_sign(
            axis=lift_axis,
            axis_key="lift",
            explicit_sign=args.lift_effect_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        depth_ready = (not args.enable_depth) or _apply_cartesian_sign(
            axis=depth_axis,
            axis_key="depth",
            explicit_sign=args.depth_effect_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        next_motion_at = 0.0
        preferred_motion_mode = "joint"
        search_state = {
            "direction": 1.0,
            "step_deg": float(args.search_pan_step_deg),
            "min_deg": float(pan_axis.min_deg if pan_axis is not None else 0.0),
            "max_deg": float(pan_axis.max_deg if pan_axis is not None else 0.0),
            "steps": 0,
        }
        _log(
            "runtime motion mode="
            + ("blocking" if wait_for_motion else f"non-blocking interval={command_interval_sec:.3f}s")
        )

        try:
            while True:
                payload = client.get_latest()
                last_payload = payload
                now_monotonic = time.monotonic()
                timestamp = float(payload.get("timestamp") or 0.0)
                age_sec = max(0.0, time.time() - timestamp)
                if age_sec > args.max_face_staleness_sec:
                    misses += 1
                    no_face_streak += 1
                    _log(f"skip stale frame: age={age_sec:.2f}s")
                    if no_face_streak >= int(args.search_miss_threshold) and (wait_for_motion or now_monotonic >= next_motion_at):
                        current_state = arm.get_state()
                        current_joint_state = dict(current_state["joint_state"])
                        search_result, moved = _apply_search_step(
                            arm,
                            current_joint_state=current_joint_state,
                            pan_axis=pan_axis,
                            search_state=search_state,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=wait_for_motion,
                        )
                        if moved:
                            if wait_for_motion and search_result is not None:
                                current_state = dict(search_result)
                                current_joint_state = dict(search_result["joint_state"])
                            if not wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue
                if payload.get("status") != "tracking" or not bool(payload.get("detected")):
                    misses += 1
                    no_face_streak += 1
                    _log(f"skip no-face frame: status={payload.get('status')} detected={payload.get('detected')}")
                    if no_face_streak >= int(args.search_miss_threshold) and (wait_for_motion or now_monotonic >= next_motion_at):
                        current_state = arm.get_state()
                        current_joint_state = dict(current_state["joint_state"])
                        search_result, moved = _apply_search_step(
                            arm,
                            current_joint_state=current_joint_state,
                            pan_axis=pan_axis,
                            search_state=search_state,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=wait_for_motion,
                        )
                        if moved:
                            if wait_for_motion and search_result is not None:
                                current_state = dict(search_result)
                                current_joint_state = dict(search_result["joint_state"])
                            if not wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue

                no_face_streak = 0

                if pan_axis is not None and not pan_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            arm,
                            client,
                            pan_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            face_timeout_sec=args.face_timeout_sec,
                            max_face_staleness_sec=args.max_face_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        current_state = arm.get_state()
                        sign_cache["pan"] = float(pan_axis.control_sign)
                        _save_sign_cache(sign_cache)
                        pan_ready = True
                        _log(
                            f"axis ready: joint={pan_axis.joint_name}, metric={pan_axis.metric_key}, "
                            f"control_sign={pan_axis.control_sign:+.1f}, range=[{pan_axis.min_deg:.2f}, {pan_axis.max_deg:.2f}]"
                        )
                        payload = client.get_latest()
                        last_payload = payload
                    except Exception as exc:
                        _warn(f"disable pan axis ({pan_axis.joint_name}): {exc}")
                        pan_axis = None
                        pan_ready = True

                if tilt_axis is not None and not tilt_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            arm,
                            client,
                            tilt_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            face_timeout_sec=args.face_timeout_sec,
                            max_face_staleness_sec=args.max_face_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        current_state = arm.get_state()
                        sign_cache["tilt_primary"] = float(tilt_axis.control_sign)
                        _save_sign_cache(sign_cache)
                        tilt_ready = True
                        _log(
                            f"axis ready: joint={tilt_axis.joint_name}, metric={tilt_axis.metric_key}, "
                            f"control_sign={tilt_axis.control_sign:+.1f}, range=[{tilt_axis.min_deg:.2f}, {tilt_axis.max_deg:.2f}]"
                        )
                        payload = client.get_latest()
                        last_payload = payload
                    except Exception as exc:
                        _warn(f"disable tilt axis ({tilt_axis.joint_name}): {exc}")
                        tilt_axis = None
                        tilt_ready = True

                if tilt_secondary_axis is not None and not tilt_secondary_ready:
                    try:
                        probe_report = _probe_axis_sign(
                            arm,
                            client,
                            tilt_secondary_axis,
                            probe_delta_deg=args.probe_delta_deg,
                            move_duration_sec=args.move_duration_sec,
                            face_timeout_sec=args.face_timeout_sec,
                            max_face_staleness_sec=args.max_face_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_joint_state = dict(probe_report["revert_joint_state"])
                        current_state = arm.get_state()
                        sign_cache["tilt_secondary"] = float(tilt_secondary_axis.control_sign)
                        _save_sign_cache(sign_cache)
                        tilt_secondary_ready = True
                        _log(
                            f"axis ready: joint={tilt_secondary_axis.joint_name}, metric={tilt_secondary_axis.metric_key}, "
                            f"control_sign={tilt_secondary_axis.control_sign:+.1f}, "
                            f"range=[{tilt_secondary_axis.min_deg:.2f}, {tilt_secondary_axis.max_deg:.2f}]"
                        )
                        payload = client.get_latest()
                        last_payload = payload
                    except Exception as exc:
                        _warn(f"disable secondary tilt axis ({tilt_secondary_axis.joint_name}): {exc}")
                        tilt_secondary_axis = None
                        tilt_secondary_ready = True

                if args.enable_lift and not lift_ready:
                    try:
                        probe_report = _probe_cartesian_axis_sign(
                            arm,
                            client,
                            lift_axis,
                            probe_step=max(float(args.lift_max_step_m), float(args.min_cartesian_step_m)),
                            move_duration_sec=args.move_duration_sec,
                            face_timeout_sec=args.face_timeout_sec,
                            max_face_staleness_sec=args.max_face_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_state = dict(probe_report["revert_state"])
                        current_joint_state = dict(current_state["joint_state"])
                        sign_cache["lift"] = float(lift_axis.effect_sign)
                        _save_sign_cache(sign_cache)
                        lift_ready = True
                        _log(
                            f"axis ready: axis={lift_axis.name}, metric={lift_axis.metric_key}, "
                            f"effect_sign={lift_axis.effect_sign:+.1f}, range=[{lift_axis.min_value:.4f}, {lift_axis.max_value:.4f}]"
                        )
                        payload = client.get_latest()
                        last_payload = payload
                    except Exception as exc:
                        _warn(f"disable lift axis ({lift_axis.name}): {exc}")
                        lift_ready = True
                        args.enable_lift = False

                if args.enable_depth and not depth_ready:
                    try:
                        probe_report = _probe_cartesian_axis_sign(
                            arm,
                            client,
                            depth_axis,
                            probe_step=max(float(args.depth_max_step_m), float(args.min_cartesian_step_m)),
                            move_duration_sec=args.move_duration_sec,
                            face_timeout_sec=args.face_timeout_sec,
                            max_face_staleness_sec=args.max_face_staleness_sec,
                            min_probe_metric_delta=args.min_probe_metric_delta,
                        )
                        calibration.append(probe_report)
                        current_state = dict(probe_report["revert_state"])
                        current_joint_state = dict(current_state["joint_state"])
                        sign_cache["depth"] = float(depth_axis.effect_sign)
                        _save_sign_cache(sign_cache)
                        depth_ready = True
                        _log(
                            f"axis ready: axis={depth_axis.name}, metric={depth_axis.metric_key}, "
                            f"effect_sign={depth_axis.effect_sign:+.1f}, range=[{depth_axis.min_value:.4f}, {depth_axis.max_value:.4f}]"
                        )
                        payload = client.get_latest()
                        last_payload = payload
                    except Exception as exc:
                        _warn(f"disable depth axis ({depth_axis.name}): {exc}")
                        depth_ready = True
                        args.enable_depth = False

                now_monotonic = time.monotonic()
                if not wait_for_motion and now_monotonic < next_motion_at:
                    time.sleep(args.poll_interval_sec)
                    continue

                current_state = arm.get_state()
                current_joint_state = dict(current_state["joint_state"])

                targets: Dict[str, float] = {}
                ndx = float((payload.get("smoothed_offset") or {}).get("ndx", 0.0))
                ndy = float((payload.get("smoothed_offset") or {}).get("ndy", 0.0))
                area_ratio = _extract_face_metric(payload, "area_ratio")
                if pan_axis is not None:
                    next_target = pan_axis.compute_next_target(float(current_joint_state[pan_axis.joint_name]), ndx)
                    if next_target is not None and abs(next_target - float(current_joint_state[pan_axis.joint_name])) >= args.min_command_deg:
                        targets[pan_axis.joint_name] = next_target
                if tilt_axis is not None:
                    next_target = tilt_axis.compute_next_target(float(current_joint_state[tilt_axis.joint_name]), ndy)
                    if next_target is not None and abs(next_target - float(current_joint_state[tilt_axis.joint_name])) >= args.min_command_deg:
                        targets[tilt_axis.joint_name] = next_target
                if tilt_secondary_axis is not None:
                    next_target = tilt_secondary_axis.compute_next_target(
                        float(current_joint_state[tilt_secondary_axis.joint_name]),
                        ndy,
                    )
                    if (
                        next_target is not None
                        and abs(next_target - float(current_joint_state[tilt_secondary_axis.joint_name])) >= args.min_command_deg
                    ):
                        targets[tilt_secondary_axis.joint_name] = next_target

                delta_dx = 0.0
                delta_dz = 0.0
                if args.enable_lift:
                    current_z = lift_axis.current_value(current_state)
                    lift_step = lift_axis.compute_step(ndy, target_value=0.0) if abs(ndy) > args.dead_zone_ndy else 0.0
                    target_z = _clamp(current_z + lift_step, lift_axis.min_value, lift_axis.max_value)
                    delta_dz = target_z - current_z
                    if abs(delta_dz) < args.min_cartesian_step_m:
                        delta_dz = 0.0

                if args.enable_depth:
                    area_error = depth_target_area_ratio - area_ratio
                    if abs(area_error) <= float(args.depth_area_dead_zone):
                        area_error = 0.0
                    current_x = depth_axis.current_value(current_state)
                    depth_step = (
                        depth_axis.compute_step(area_ratio, target_value=depth_target_area_ratio)
                        if abs(area_error) > 1e-9
                        else 0.0
                    )
                    target_x = _clamp(current_x + depth_step, depth_axis.min_value, depth_axis.max_value)
                    delta_dx = target_x - current_x
                    if abs(delta_dx) < args.min_cartesian_step_m:
                        delta_dx = 0.0

                iterations += 1
                if not targets and abs(delta_dx) < 1e-12 and abs(delta_dz) < 1e-12:
                    _log(
                        f"hold iter={iterations} frame={payload.get('frame_id')} "
                        f"ndx={ndx:+.4f} ndy={ndy:+.4f} area={area_ratio:.4f}"
                    )
                    time.sleep(args.poll_interval_sec)
                    continue

                log_parts = []
                if wait_for_motion:
                    if targets:
                        result = arm.move_joints(
                            targets_deg=targets,
                            duration=args.move_duration_sec,
                            wait=True,
                        )
                        current_state = result["state"]
                        current_joint_state = dict(result["state"]["joint_state"])
                        commands_sent += 1
                        log_parts.append(f"targets={json.dumps(targets, ensure_ascii=False, sort_keys=True)}")
                    if abs(delta_dx) >= 1e-12 or abs(delta_dz) >= 1e-12:
                        result = arm.move_delta(
                            dx=delta_dx,
                            dz=delta_dz,
                            frame="urdf",
                            duration=args.move_duration_sec,
                            wait=True,
                        )
                        current_state = result["state"]
                        current_joint_state = dict(result["state"]["joint_state"])
                        commands_sent += 1
                        log_parts.append(f"delta={{\"dx\": {delta_dx:+.4f}, \"dz\": {delta_dz:+.4f}}}")
                else:
                    mode, preferred_motion_mode = _select_motion_mode(
                        has_joint_targets=bool(targets),
                        has_cartesian_delta=abs(delta_dx) >= 1e-12 or abs(delta_dz) >= 1e-12,
                        preferred_mode=preferred_motion_mode,
                    )
                    if mode == "joint":
                        arm.move_joints(
                            targets_deg=targets,
                            duration=args.move_duration_sec,
                            wait=False,
                        )
                        commands_sent += 1
                        next_motion_at = time.monotonic() + command_interval_sec
                        log_parts.append(f"mode={mode}")
                        log_parts.append(f"targets={json.dumps(targets, ensure_ascii=False, sort_keys=True)}")
                        if abs(delta_dx) >= 1e-12 or abs(delta_dz) >= 1e-12:
                            log_parts.append("deferred=cartesian")
                    elif mode == "cartesian":
                        arm.move_delta(
                            dx=delta_dx,
                            dz=delta_dz,
                            frame="urdf",
                            duration=args.move_duration_sec,
                            wait=False,
                        )
                        commands_sent += 1
                        next_motion_at = time.monotonic() + command_interval_sec
                        log_parts.append(f"mode={mode}")
                        log_parts.append(f"delta={{\"dx\": {delta_dx:+.4f}, \"dz\": {delta_dz:+.4f}}}")
                        if targets:
                            log_parts.append("deferred=joint")
                _log(
                    f"move iter={iterations} frame={payload.get('frame_id')} ndx={ndx:+.4f} ndy={ndy:+.4f} area={area_ratio:.4f} "
                    + " ".join(log_parts)
                )
        except KeyboardInterrupt:
            interrupted = True
            _log("received Ctrl+C; stopping face follow loop")

        if args.hold_on_exit:
            hold_result = arm.stop()
            current_joint_state = dict(hold_result["state"]["joint_state"])

        return {
            "action": "face_follow",
            "face_endpoint": client.latest_url,
            "status_endpoint": client.status_url,
            "calibration": calibration,
            "start_joint_state": start_state["joint_state"],
            "final_joint_state": current_joint_state,
            "iterations": iterations,
            "commands_sent": commands_sent,
            "search_steps": search_steps,
            "misses": misses,
            "stopped_by_user": interrupted,
            "last_face_payload": last_payload,
            "wait_for_motion": wait_for_motion,
            "command_interval_sec": command_interval_sec,
            "axes": {
                "pan": None
                if pan_axis is None
                else {
                    "joint": pan_axis.joint_name,
                    "metric": pan_axis.metric_key,
                    "control_sign": pan_axis.control_sign,
                    "dead_zone_norm": pan_axis.dead_zone_norm,
                    "gain_deg_per_norm": pan_axis.gain_deg_per_norm,
                    "max_step_deg": pan_axis.max_step_deg,
                    "min_deg": pan_axis.min_deg,
                    "max_deg": pan_axis.max_deg,
                },
                "tilt": None
                if tilt_axis is None
                else {
                    "joint": tilt_axis.joint_name,
                    "metric": tilt_axis.metric_key,
                    "control_sign": tilt_axis.control_sign,
                    "dead_zone_norm": tilt_axis.dead_zone_norm,
                    "gain_deg_per_norm": tilt_axis.gain_deg_per_norm,
                    "max_step_deg": tilt_axis.max_step_deg,
                    "min_deg": tilt_axis.min_deg,
                    "max_deg": tilt_axis.max_deg,
                },
                "tilt_secondary": None
                if tilt_secondary_axis is None
                else {
                    "joint": tilt_secondary_axis.joint_name,
                    "metric": tilt_secondary_axis.metric_key,
                    "control_sign": tilt_secondary_axis.control_sign,
                    "dead_zone_norm": tilt_secondary_axis.dead_zone_norm,
                    "gain_deg_per_norm": tilt_secondary_axis.gain_deg_per_norm,
                    "max_step_deg": tilt_secondary_axis.max_step_deg,
                    "min_deg": tilt_secondary_axis.min_deg,
                    "max_deg": tilt_secondary_axis.max_deg,
                },
                "lift": None
                if not args.enable_lift
                else {
                    "axis": lift_axis.name,
                    "metric": lift_axis.metric_key,
                    "effect_sign": lift_axis.effect_sign,
                    "component": lift_axis.component,
                    "gain_per_metric": lift_axis.gain_per_metric,
                    "max_step": lift_axis.max_step,
                    "min_value": lift_axis.min_value,
                    "max_value": lift_axis.max_value,
                },
                "depth": None
                if not args.enable_depth
                else {
                    "axis": depth_axis.name,
                    "metric": depth_axis.metric_key,
                    "effect_sign": depth_axis.effect_sign,
                    "component": depth_axis.component,
                    "gain_per_metric": depth_axis.gain_per_metric,
                    "max_step": depth_axis.max_step,
                    "min_value": depth_axis.min_value,
                    "max_value": depth_axis.max_value,
                    "min_area_ratio": args.depth_min_area_ratio,
                    "max_area_ratio": args.depth_max_area_ratio,
                    "target_area_ratio": depth_target_area_ratio,
                    "area_dead_zone": args.depth_area_dead_zone,
                },
            },
        }
    finally:
        arm.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Keep a detected face near the frame center with soarmMoce")
    parser.add_argument("--face-endpoint", default="http://127.0.0.1:8011", help="Face tracking service base URL or /latest URL")
    parser.add_argument("--http-timeout-sec", type=float, default=1.5)
    parser.add_argument("--poll-interval-sec", type=float, default=0.02)
    parser.add_argument("--move-duration-sec", type=float, default=0.12)
    parser.add_argument("--face-timeout-sec", type=float, default=3.0, help="Wait timeout when probing control sign")
    parser.add_argument("--max-face-staleness-sec", type=float, default=1.5)
    parser.add_argument("--min-probe-metric-delta", type=float, default=0.005)
    parser.add_argument("--probe-delta-deg", type=float, default=1.5)
    parser.add_argument("--probe-failure-policy", default="skip-optional", help="strict | disable-axis | skip-optional")
    parser.add_argument("--reprobe-control-signs", action="store_true", help="Ignore cached face-follow signs and probe again")
    parser.add_argument("--pan-control-sign", default="1", help="auto | 1 | -1")
    parser.add_argument("--tilt-control-sign", default="1", help="auto | 1 | -1")
    parser.add_argument("--tilt-secondary-control-sign", default="-1", help="auto | 1 | -1")
    parser.add_argument("--lift-effect-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--depth-effect-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--pan-joint", default="shoulder_pan", choices=JOINTS)
    parser.add_argument("--tilt-joint", default="shoulder_lift", help="Primary vertical joint or 'none' to disable")
    parser.add_argument("--tilt-secondary-joint", default="elbow_flex", help="Secondary vertical joint or 'none' to disable")
    parser.add_argument("--pan-range-deg", type=float, default=18.0, help="Allowed pan motion around startup pose")
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
    parser.add_argument("--enable-lift", type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"}, default=False)
    parser.add_argument("--enable-depth", type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"}, default=False)
    parser.add_argument("--lift-range-m", type=float, default=0.03)
    parser.add_argument("--depth-range-m", type=float, default=0.04)
    parser.add_argument("--lift-gain-m-per-norm", type=float, default=0.028)
    parser.add_argument("--depth-gain-m-per-area", type=float, default=0.14)
    parser.add_argument("--lift-max-step-m", type=float, default=0.008)
    parser.add_argument("--depth-max-step-m", type=float, default=0.012)
    parser.add_argument("--min-cartesian-step-m", type=float, default=0.0010)
    parser.add_argument("--depth-min-area-ratio", type=float, default=0.10)
    parser.add_argument("--depth-max-area-ratio", type=float, default=0.28)
    parser.add_argument("--depth-target-area-ratio", type=float, default=None, help="Desired face area ratio; default is midpoint of min/max")
    parser.add_argument("--depth-area-dead-zone", type=float, default=0.025, help="No depth move when face area is within this distance of target")
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
    parser.add_argument("--hold-on-exit", type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"}, default=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.tilt_joint = _normalize_optional_joint(args.tilt_joint)
        args.tilt_secondary_joint = _normalize_optional_joint(args.tilt_secondary_joint)
        args.pan_control_sign = _normalize_sign_arg(args.pan_control_sign, "--pan-control-sign")
        args.tilt_control_sign = _normalize_sign_arg(args.tilt_control_sign, "--tilt-control-sign")
        args.tilt_secondary_control_sign = _normalize_sign_arg(
            args.tilt_secondary_control_sign,
            "--tilt-secondary-control-sign",
        )
        args.lift_effect_sign = _normalize_sign_arg(args.lift_effect_sign, "--lift-effect-sign")
        args.depth_effect_sign = _normalize_sign_arg(args.depth_effect_sign, "--depth-effect-sign")
        args.probe_failure_policy = _normalize_probe_policy(args.probe_failure_policy)
        print_success(run_face_follow(args))
    except KeyboardInterrupt as exc:
        print_error(exc)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
