#!/usr/bin/env python3
"""Keep a detected face near the camera center using Cartesian IK on the real soarmMoce arm."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import SoArmMoceController, ValidationError


SIGN_CACHE_PATH = Path(__file__).resolve().parents[1] / "calibration" / "face_follow_ik_signs.json"


def _log(message: str) -> None:
    print(f"[face-follow-ik] {message}", file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    print(f"[face-follow-ik][warn] {message}", file=sys.stderr, flush=True)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _build_delta_kwargs(component: str, value: float) -> dict[str, float]:
    delta_kwargs = {"dx": 0.0, "dy": 0.0, "dz": 0.0}
    delta_key = f"d{component}"
    if delta_key not in delta_kwargs:
        raise ValueError(f"Unsupported cartesian component: {component}")
    delta_kwargs[delta_key] = float(value)
    return delta_kwargs


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


def _apply_axis_sign(
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
    motion_frame: str,
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
        delta_kwargs = _build_delta_kwargs(axis.component, effective_probe)
        _log(
            f"probing {axis.name} on {axis.metric_key}: baseline={baseline_metric:+.4f}, "
            f"{axis.component}+={effective_probe:.4f}m frame={motion_frame}"
        )
        move_plus = arm.move_delta(
            dx=delta_kwargs["dx"],
            dy=delta_kwargs["dy"],
            dz=delta_kwargs["dz"],
            frame=motion_frame,
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
                frame=motion_frame,
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


def _apply_search_step(
    arm: SoArmMoceController,
    *,
    current_state: dict[str, Any],
    axis: CartesianAxis | None,
    search_state: dict[str, Any],
    motion_frame: str,
    move_duration_sec: float,
    wait_for_motion: bool,
    min_cartesian_step_m: float,
) -> tuple[dict[str, Any] | None, bool]:
    if axis is None:
        return None, False

    direction = float(search_state.get("direction", 1.0))
    step_m = abs(float(search_state.get("step_m", axis.max_step)))
    min_value = float(search_state.get("min_value", axis.min_value))
    max_value = float(search_state.get("max_value", axis.max_value))
    current_value = axis.current_value(current_state)
    target_value = current_value + direction * step_m

    bounced = False
    if target_value > max_value:
        direction = -1.0
        target_value = max_value
        bounced = True
    elif target_value < min_value:
        direction = 1.0
        target_value = min_value
        bounced = True

    delta_value = target_value - current_value
    if abs(delta_value) < max(1e-6, float(min_cartesian_step_m)):
        search_state["direction"] = -direction
        return None, False

    delta_kwargs = _build_delta_kwargs(axis.component, delta_value)
    result = arm.move_delta(
        dx=delta_kwargs["dx"],
        dy=delta_kwargs["dy"],
        dz=delta_kwargs["dz"],
        frame=motion_frame,
        duration=move_duration_sec,
        wait=wait_for_motion,
    )
    search_state["direction"] = direction if not bounced else -direction
    search_state["steps"] = int(search_state.get("steps", 0)) + 1
    return result["state"], True


def run_face_follow_ik(args: argparse.Namespace) -> dict[str, Any]:
    if float(args.poll_interval_sec) < 0.0:
        raise ValidationError("--poll-interval-sec must be >= 0")
    if float(args.move_duration_sec) <= 0.0:
        raise ValidationError("--move-duration-sec must be > 0")
    if float(args.max_face_staleness_sec) < 0.0:
        raise ValidationError("--max-face-staleness-sec must be >= 0")
    if float(args.min_cartesian_step_m) < 0.0:
        raise ValidationError("--min-cartesian-step-m must be >= 0")
    if float(args.depth_area_dead_zone) < 0.0:
        raise ValidationError("--depth-area-dead-zone must be >= 0")
    if int(args.search_miss_threshold) < 0:
        raise ValidationError("--search-miss-threshold must be >= 0")
    if float(args.search_lateral_step_m) <= 0.0:
        raise ValidationError("--search-lateral-step-m must be > 0")
    if args.command_interval_sec is not None and float(args.command_interval_sec) < 0.0:
        raise ValidationError("--command-interval-sec must be >= 0")

    client = FaceTrackingClient(args.face_endpoint, timeout_sec=args.http_timeout_sec)
    status = client.get_status()
    if not bool(status.get("running", status.get("engine_running", False))):
        raise RuntimeError(f"Face tracking service is not running: {status}")

    arm = SoArmMoceController()
    start_state = arm.get_state()
    current_state = dict(start_state)
    current_tcp = {
        "x": float(start_state["tcp_pose"]["xyz"][0]),
        "y": float(start_state["tcp_pose"]["xyz"][1]),
        "z": float(start_state["tcp_pose"]["xyz"][2]),
    }

    lateral_axis = CartesianAxis(
        name="lateral_y",
        metric_key="ndx",
        component="y",
        gain_per_metric=float(args.lateral_gain_m_per_norm),
        max_step=float(args.lateral_max_step_m),
        min_value=float(current_tcp["y"]) - float(args.lateral_range_m),
        max_value=float(current_tcp["y"]) + float(args.lateral_range_m),
    )
    vertical_axis = CartesianAxis(
        name="vertical_z",
        metric_key="ndy",
        component="z",
        gain_per_metric=float(args.vertical_gain_m_per_norm),
        max_step=float(args.vertical_max_step_m),
        min_value=float(current_tcp["z"]) - float(args.vertical_range_m),
        max_value=float(current_tcp["z"]) + float(args.vertical_range_m),
    )
    depth_axis = CartesianAxis(
        name="depth_x",
        metric_key="area_ratio",
        component="x",
        gain_per_metric=float(args.depth_gain_m_per_area),
        max_step=float(args.depth_max_step_m),
        min_value=float(current_tcp["x"]) - float(args.depth_range_m),
        max_value=float(current_tcp["x"]) + float(args.depth_range_m),
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

    calibration: list[dict[str, Any]] = []
    try:
        lateral_ready = _apply_axis_sign(
            axis=lateral_axis,
            axis_key="lateral",
            explicit_sign=args.lateral_effect_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        vertical_ready = _apply_axis_sign(
            axis=vertical_axis,
            axis_key="vertical",
            explicit_sign=args.vertical_effect_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )
        depth_ready = (not args.enable_depth) or _apply_axis_sign(
            axis=depth_axis,
            axis_key="depth",
            explicit_sign=args.depth_effect_sign,
            use_cache=not args.reprobe_control_signs,
            sign_cache=sign_cache,
            calibration=calibration,
        )

        if not lateral_ready:
            probe_report = _probe_cartesian_axis_sign(
                arm,
                client,
                lateral_axis,
                probe_step=args.probe_step_m,
                move_duration_sec=args.move_duration_sec,
                face_timeout_sec=args.face_timeout_sec,
                max_face_staleness_sec=args.max_face_staleness_sec,
                min_probe_metric_delta=args.min_probe_metric_delta,
                motion_frame=args.motion_frame,
            )
            sign_cache["lateral"] = float(lateral_axis.effect_sign)
            _save_sign_cache(sign_cache)
            calibration.append({**probe_report, "mode": "probe", "cache_key": "lateral"})
            _log(
                f"axis ready: axis={lateral_axis.name}, metric={lateral_axis.metric_key}, "
                f"effect_sign={lateral_axis.effect_sign:+.1f}, range=[{lateral_axis.min_value:.4f}, {lateral_axis.max_value:.4f}]"
            )
        if not vertical_ready:
            probe_report = _probe_cartesian_axis_sign(
                arm,
                client,
                vertical_axis,
                probe_step=args.probe_step_m,
                move_duration_sec=args.move_duration_sec,
                face_timeout_sec=args.face_timeout_sec,
                max_face_staleness_sec=args.max_face_staleness_sec,
                min_probe_metric_delta=args.min_probe_metric_delta,
                motion_frame=args.motion_frame,
            )
            sign_cache["vertical"] = float(vertical_axis.effect_sign)
            _save_sign_cache(sign_cache)
            calibration.append({**probe_report, "mode": "probe", "cache_key": "vertical"})
            _log(
                f"axis ready: axis={vertical_axis.name}, metric={vertical_axis.metric_key}, "
                f"effect_sign={vertical_axis.effect_sign:+.1f}, range=[{vertical_axis.min_value:.4f}, {vertical_axis.max_value:.4f}]"
            )
        if args.enable_depth and not depth_ready:
            try:
                probe_report = _probe_cartesian_axis_sign(
                    arm,
                    client,
                    depth_axis,
                    probe_step=args.probe_step_m,
                    move_duration_sec=args.move_duration_sec,
                    face_timeout_sec=args.face_timeout_sec,
                    max_face_staleness_sec=args.max_face_staleness_sec,
                    min_probe_metric_delta=args.min_probe_metric_delta,
                    motion_frame=args.motion_frame,
                )
                sign_cache["depth"] = float(depth_axis.effect_sign)
                _save_sign_cache(sign_cache)
                calibration.append({**probe_report, "mode": "probe", "cache_key": "depth"})
                _log(
                    f"axis ready: axis={depth_axis.name}, metric={depth_axis.metric_key}, "
                    f"effect_sign={depth_axis.effect_sign:+.1f}, range=[{depth_axis.min_value:.4f}, {depth_axis.max_value:.4f}]"
                )
            except Exception as exc:
                _warn(f"disable depth axis ({depth_axis.name}): {exc}")
                depth_ready = True
                args.enable_depth = False

        iterations = 0
        misses = 0
        commands_sent = 0
        search_steps = 0
        no_face_streak = 0
        interrupted = False
        next_motion_at = 0.0
        last_payload: dict[str, Any] | None = None
        search_state = {
            "direction": 1.0,
            "step_m": float(args.search_lateral_step_m),
            "min_value": float(lateral_axis.min_value),
            "max_value": float(lateral_axis.max_value),
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
                    if (
                        int(args.search_miss_threshold) > 0
                        and no_face_streak >= int(args.search_miss_threshold)
                        and (wait_for_motion or now_monotonic >= next_motion_at)
                    ):
                        current_state = arm.get_state()
                        search_result, moved = _apply_search_step(
                            arm,
                            current_state=current_state,
                            axis=lateral_axis,
                            search_state=search_state,
                            motion_frame=args.motion_frame,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=wait_for_motion,
                            min_cartesian_step_m=args.min_cartesian_step_m,
                        )
                        if moved:
                            if wait_for_motion and search_result is not None:
                                current_state = dict(search_result)
                            if not wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            _log(
                                f"search step={search_steps} streak={no_face_streak} "
                                f"component={lateral_axis.component} direction={search_state['direction']:+.1f}"
                            )
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue
                if payload.get("status") != "tracking" or not bool(payload.get("detected")):
                    misses += 1
                    no_face_streak += 1
                    _log(f"skip no-face frame: status={payload.get('status')} detected={payload.get('detected')}")
                    if (
                        int(args.search_miss_threshold) > 0
                        and no_face_streak >= int(args.search_miss_threshold)
                        and (wait_for_motion or now_monotonic >= next_motion_at)
                    ):
                        current_state = arm.get_state()
                        search_result, moved = _apply_search_step(
                            arm,
                            current_state=current_state,
                            axis=lateral_axis,
                            search_state=search_state,
                            motion_frame=args.motion_frame,
                            move_duration_sec=args.move_duration_sec,
                            wait_for_motion=wait_for_motion,
                            min_cartesian_step_m=args.min_cartesian_step_m,
                        )
                        if moved:
                            if wait_for_motion and search_result is not None:
                                current_state = dict(search_result)
                            if not wait_for_motion:
                                next_motion_at = time.monotonic() + command_interval_sec
                            commands_sent += 1
                            search_steps += 1
                            _log(
                                f"search step={search_steps} streak={no_face_streak} "
                                f"component={lateral_axis.component} direction={search_state['direction']:+.1f}"
                            )
                            continue
                    time.sleep(args.poll_interval_sec)
                    continue

                no_face_streak = 0

                if not wait_for_motion and now_monotonic < next_motion_at:
                    time.sleep(args.poll_interval_sec)
                    continue

                current_state = arm.get_state()
                ndx = float((payload.get("smoothed_offset") or {}).get("ndx", 0.0))
                ndy = float((payload.get("smoothed_offset") or {}).get("ndy", 0.0))
                area_ratio = _extract_face_metric(payload, "area_ratio")

                current_y = lateral_axis.current_value(current_state)
                lateral_step = lateral_axis.compute_step(ndx, target_value=0.0) if abs(ndx) > args.dead_zone_ndx else 0.0
                target_y = _clamp(current_y + lateral_step, lateral_axis.min_value, lateral_axis.max_value)
                delta_dy = target_y - current_y
                if abs(delta_dy) < args.min_cartesian_step_m:
                    delta_dy = 0.0

                current_z = vertical_axis.current_value(current_state)
                vertical_step = vertical_axis.compute_step(ndy, target_value=0.0) if abs(ndy) > args.dead_zone_ndy else 0.0
                target_z = _clamp(current_z + vertical_step, vertical_axis.min_value, vertical_axis.max_value)
                delta_dz = target_z - current_z
                if abs(delta_dz) < args.min_cartesian_step_m:
                    delta_dz = 0.0

                delta_dx = 0.0
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
                if abs(delta_dx) < 1e-12 and abs(delta_dy) < 1e-12 and abs(delta_dz) < 1e-12:
                    _log(
                        f"hold iter={iterations} frame={payload.get('frame_id')} "
                        f"ndx={ndx:+.4f} ndy={ndy:+.4f} area={area_ratio:.4f}"
                    )
                    time.sleep(args.poll_interval_sec)
                    continue

                if wait_for_motion:
                    current_state = arm.move_delta(
                        dx=delta_dx,
                        dy=delta_dy,
                        dz=delta_dz,
                        frame=args.motion_frame,
                        duration=args.move_duration_sec,
                        wait=True,
                    )["state"]
                    commands_sent += 1
                else:
                    arm.move_delta(
                        dx=delta_dx,
                        dy=delta_dy,
                        dz=delta_dz,
                        frame=args.motion_frame,
                        duration=args.move_duration_sec,
                        wait=False,
                    )
                    commands_sent += 1
                    next_motion_at = time.monotonic() + command_interval_sec

                _log(
                    f"move iter={iterations} frame={payload.get('frame_id')} ndx={ndx:+.4f} ndy={ndy:+.4f} area={area_ratio:.4f} "
                    f"delta={{\"dx\": {delta_dx:+.4f}, \"dy\": {delta_dy:+.4f}, \"dz\": {delta_dz:+.4f}}}"
                )
        except KeyboardInterrupt:
            interrupted = True
            _log("received Ctrl+C; stopping face follow loop")

        if args.hold_on_exit:
            current_state = arm.stop()["state"]

        return {
            "action": "face_follow_ik",
            "face_endpoint": client.latest_url,
            "status_endpoint": client.status_url,
            "motion_frame": args.motion_frame,
            "calibration": calibration,
            "start_joint_state": start_state["joint_state"],
            "start_tcp": current_tcp,
            "final_joint_state": current_state["joint_state"],
            "final_tcp": {
                "x": float(current_state["tcp_pose"]["xyz"][0]),
                "y": float(current_state["tcp_pose"]["xyz"][1]),
                "z": float(current_state["tcp_pose"]["xyz"][2]),
            },
            "iterations": iterations,
            "commands_sent": commands_sent,
            "misses": misses,
            "search_steps": search_steps,
            "stopped_by_user": interrupted,
            "last_face_payload": last_payload,
            "wait_for_motion": wait_for_motion,
            "command_interval_sec": command_interval_sec,
            "axes": {
                "lateral": {
                    "axis": lateral_axis.name,
                    "metric": lateral_axis.metric_key,
                    "effect_sign": lateral_axis.effect_sign,
                    "component": lateral_axis.component,
                    "gain_per_metric": lateral_axis.gain_per_metric,
                    "max_step": lateral_axis.max_step,
                    "min_value": lateral_axis.min_value,
                    "max_value": lateral_axis.max_value,
                },
                "vertical": {
                    "axis": vertical_axis.name,
                    "metric": vertical_axis.metric_key,
                    "effect_sign": vertical_axis.effect_sign,
                    "component": vertical_axis.component,
                    "gain_per_metric": vertical_axis.gain_per_metric,
                    "max_step": vertical_axis.max_step,
                    "min_value": vertical_axis.min_value,
                    "max_value": vertical_axis.max_value,
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
    parser = argparse.ArgumentParser(description="Keep a detected face near the frame center with IK/cartesian motion")
    parser.add_argument("--face-endpoint", default="http://127.0.0.1:8011", help="Face tracking service base URL or /latest URL")
    parser.add_argument("--motion-frame", choices=["base", "urdf"], default="urdf")
    parser.add_argument("--http-timeout-sec", type=float, default=1.5)
    parser.add_argument("--poll-interval-sec", type=float, default=0.02)
    parser.add_argument("--move-duration-sec", type=float, default=0.14)
    parser.add_argument("--face-timeout-sec", type=float, default=3.0, help="Wait timeout when probing axis sign")
    parser.add_argument("--max-face-staleness-sec", type=float, default=1.5)
    parser.add_argument("--min-probe-metric-delta", type=float, default=0.005)
    parser.add_argument("--probe-step-m", type=float, default=0.006)
    parser.add_argument("--reprobe-control-signs", action="store_true", help="Ignore cached IK face-follow signs and probe again")
    parser.add_argument("--lateral-effect-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--vertical-effect-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--depth-effect-sign", default="auto", help="auto | 1 | -1")
    parser.add_argument("--lateral-range-m", type=float, default=0.08)
    parser.add_argument("--vertical-range-m", type=float, default=0.06)
    parser.add_argument("--depth-range-m", type=float, default=0.04)
    parser.add_argument("--lateral-gain-m-per-norm", type=float, default=0.030)
    parser.add_argument("--vertical-gain-m-per-norm", type=float, default=0.026)
    parser.add_argument("--depth-gain-m-per-area", type=float, default=0.14)
    parser.add_argument("--lateral-max-step-m", type=float, default=0.010)
    parser.add_argument("--vertical-max-step-m", type=float, default=0.008)
    parser.add_argument("--depth-max-step-m", type=float, default=0.012)
    parser.add_argument("--dead-zone-ndx", type=float, default=0.06)
    parser.add_argument("--dead-zone-ndy", type=float, default=0.12)
    parser.add_argument("--min-cartesian-step-m", type=float, default=0.0010)
    parser.add_argument("--enable-depth", type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"}, default=False)
    parser.add_argument("--depth-min-area-ratio", type=float, default=0.10)
    parser.add_argument("--depth-max-area-ratio", type=float, default=0.28)
    parser.add_argument("--depth-target-area-ratio", type=float, default=None, help="Desired face area ratio; default is midpoint of min/max")
    parser.add_argument("--depth-area-dead-zone", type=float, default=0.025, help="No depth move when face area is within this distance of target")
    parser.add_argument(
        "--search-miss-threshold",
        type=int,
        default=8,
        help="Start lateral search after this many consecutive stale/no-face frames; 0 disables search",
    )
    parser.add_argument("--search-lateral-step-m", type=float, default=0.010)
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
        args.lateral_effect_sign = _normalize_sign_arg(args.lateral_effect_sign, "--lateral-effect-sign")
        args.vertical_effect_sign = _normalize_sign_arg(args.vertical_effect_sign, "--vertical-effect-sign")
        args.depth_effect_sign = _normalize_sign_arg(args.depth_effect_sign, "--depth-effect-sign")
        print_success(run_face_follow_ik(args))
    except KeyboardInterrupt as exc:
        print_error(exc)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
