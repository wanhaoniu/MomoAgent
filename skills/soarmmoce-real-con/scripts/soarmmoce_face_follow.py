#!/usr/bin/env python3
"""Keep a detected face near the camera center using the real soarmMoce arm."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from soarmmoce_cli_common import print_error, print_success
from soarmmoce_sdk import JOINTS, SoArmMoceController, ValidationError


FIXED_PAN_CONTROL_SIGN = -1.0
FIXED_TILT_CONTROL_SIGN = 1.0
FIXED_TILT_SECONDARY_CONTROL_SIGN = -1.0


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


def _apply_joint_sign(
    *,
    axis: JointAxis | None,
    axis_key: str,
    explicit_sign: Optional[float],
    calibration: list[dict[str, Any]],
) -> bool:
    if axis is None:
        return True
    if explicit_sign is None:
        return False
    axis.control_sign = float(explicit_sign)
    calibration.append(
        {
            "joint": axis.joint_name,
            "metric": axis.metric_key,
            "control_sign": axis.control_sign,
            "mode": "fixed",
            "cache_key": axis_key,
        }
    )
    _log(f"axis sign fixed: joint={axis.joint_name} control_sign={axis.control_sign:+.1f}")
    return True


def _apply_cartesian_sign(
    *,
    axis: CartesianAxis,
    axis_key: str,
    explicit_sign: Optional[float],
    calibration: list[dict[str, Any]],
) -> bool:
    if explicit_sign is None:
        return False
    axis.effect_sign = float(explicit_sign)
    calibration.append(
        {
            "axis": axis.name,
            "metric": axis.metric_key,
            "effect_sign": axis.effect_sign,
            "mode": "fixed",
            "cache_key": axis_key,
        }
    )
    _log(f"axis sign fixed: axis={axis.name} effect_sign={axis.effect_sign:+.1f}")
    return True


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
    offset = payload.get("offset") or payload.get("smoothed_offset") or {}
    if metric_key not in offset:
        raise RuntimeError(f"Face payload is missing offset metric: {metric_key}")
    return float(offset[metric_key])


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
    if bool(args.home_on_start):
        _log(f"move to startup home pose: duration={float(args.home_duration_sec):.2f}s")
        home_result = arm.home(duration=float(args.home_duration_sec), wait=True)
        start_state = dict(home_result["state"])
    else:
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
            calibration=calibration,
        )
        tilt_ready = _apply_joint_sign(
            axis=tilt_axis,
            axis_key="tilt_primary",
            explicit_sign=args.tilt_control_sign,
            calibration=calibration,
        )
        tilt_secondary_ready = _apply_joint_sign(
            axis=tilt_secondary_axis,
            axis_key="tilt_secondary",
            explicit_sign=args.tilt_secondary_control_sign,
            calibration=calibration,
        )
        lift_ready = (not args.enable_lift) or _apply_cartesian_sign(
            axis=lift_axis,
            axis_key="lift",
            explicit_sign=args.lift_effect_sign,
            calibration=calibration,
        )
        depth_ready = (not args.enable_depth) or _apply_cartesian_sign(
            axis=depth_axis,
            axis_key="depth",
            explicit_sign=args.depth_effect_sign,
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
    parser.add_argument(
        "--home-on-start",
        type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"},
        default=True,
        help="Move to configured home pose before follow control",
    )
    parser.add_argument("--home-duration-sec", type=float, default=1.5)
    parser.set_defaults(
        max_face_staleness_sec=1.5,
        pan_control_sign=FIXED_PAN_CONTROL_SIGN,
        tilt_control_sign=FIXED_TILT_CONTROL_SIGN,
        tilt_secondary_control_sign=FIXED_TILT_SECONDARY_CONTROL_SIGN,
        lift_effect_sign=None,
        depth_effect_sign=None,
        pan_joint="shoulder_pan",
        tilt_joint="shoulder_lift",
        tilt_secondary_joint="elbow_flex",
        pan_range_deg=40.0,
        tilt_range_deg=18.0,
        tilt_secondary_range_deg=18.0,
        pan_gain_deg_per_norm=10.0,
        tilt_gain_deg_per_norm=4.5,
        tilt_secondary_gain_deg_per_norm=3.5,
        pan_max_step_deg=2.4,
        tilt_max_step_deg=1.2,
        tilt_secondary_max_step_deg=1.0,
        dead_zone_ndx=0.06,
        dead_zone_ndy=0.12,
        min_command_deg=0.12,
        enable_lift=False,
        enable_depth=False,
        lift_range_m=0.03,
        depth_range_m=0.04,
        lift_gain_m_per_norm=0.028,
        depth_gain_m_per_area=0.14,
        lift_max_step_m=0.008,
        depth_max_step_m=0.012,
        min_cartesian_step_m=0.0010,
        depth_min_area_ratio=0.10,
        depth_max_area_ratio=0.28,
        depth_target_area_ratio=None,
        depth_area_dead_zone=0.025,
        search_miss_threshold=1,
        search_pan_step_deg=1.6,
        wait_for_motion=False,
        command_interval_sec=None,
        hold_on_exit=True,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.tilt_joint = _normalize_optional_joint(args.tilt_joint)
        args.tilt_secondary_joint = _normalize_optional_joint(args.tilt_secondary_joint)
        print_success(run_face_follow(args))
    except KeyboardInterrupt as exc:
        print_error(exc)
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()