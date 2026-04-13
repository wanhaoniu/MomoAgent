from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
FACE_LOC_SRC = REPO_ROOT / "Software" / "Master" / "face_loc" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))
if str(FACE_LOC_SRC) not in sys.path:
    sys.path.insert(1, str(FACE_LOC_SRC))

from soarmmoce_sdk import BOUNDED_SINGLE_TURN_JOINTS, JOINTS, SoArmMoceController, resolve_config
from face_tracking.target_center import get_target_center_norm


DEFAULT_LATEST_URL = "http://127.0.0.1:8000/latest"
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Face-follow hardware loop for SoArmMoce. "
            "This script reads face tracking results from the local face_loc service "
            "and issues small joint corrections to keep the face on the configured target center."
        )
    )
    parser.add_argument("--config", type=str, default=None, help="Optional SoArmMoce SDK config yaml path.")
    parser.add_argument("--latest-url", type=str, default=DEFAULT_LATEST_URL, help="face_loc /latest endpoint.")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT_S)
    parser.add_argument("--move-duration", type=float, default=DEFAULT_MOVE_DURATION_S)
    parser.add_argument("--pan-joint", type=str, default=DEFAULT_PAN_JOINT, choices=JOINTS)
    parser.add_argument("--tilt-joint", type=str, default=DEFAULT_TILT_JOINT, choices=JOINTS)
    parser.add_argument("--pan-sign", type=float, default=DEFAULT_PAN_SIGN)
    parser.add_argument("--tilt-sign", type=float, default=DEFAULT_TILT_SIGN)
    parser.add_argument("--pan-gain", type=float, default=DEFAULT_PAN_GAIN_DEG_PER_NORM)
    parser.add_argument("--tilt-gain", type=float, default=DEFAULT_TILT_GAIN_DEG_PER_NORM)
    parser.add_argument("--pan-dead-zone", type=float, default=DEFAULT_PAN_DEAD_ZONE_NORM)
    parser.add_argument("--tilt-dead-zone", type=float, default=DEFAULT_TILT_DEAD_ZONE_NORM)
    parser.add_argument("--pan-resume-zone", type=float, default=DEFAULT_PAN_RESUME_ZONE_NORM)
    parser.add_argument("--tilt-resume-zone", type=float, default=DEFAULT_TILT_RESUME_ZONE_NORM)
    parser.add_argument("--min-pan-step", type=float, default=DEFAULT_MIN_PAN_STEP_DEG)
    parser.add_argument("--min-tilt-step", type=float, default=DEFAULT_MIN_TILT_STEP_DEG)
    parser.add_argument("--pan-min-step-zone", type=float, default=DEFAULT_PAN_MIN_STEP_ZONE_NORM)
    parser.add_argument("--tilt-min-step-zone", type=float, default=DEFAULT_TILT_MIN_STEP_ZONE_NORM)
    parser.add_argument("--max-pan-step", type=float, default=DEFAULT_MAX_PAN_STEP_DEG)
    parser.add_argument("--max-tilt-step", type=float, default=DEFAULT_MAX_TILT_STEP_DEG)
    parser.add_argument(
        "--command-mode",
        type=str,
        default=DEFAULT_COMMAND_MODE,
        choices=("stream", "settle"),
        help=(
            "stream sends non-blocking target updates for smoother motion; "
            "settle waits for each small move to finish before the next correction."
        ),
    )
    parser.add_argument(
        "--limit-margin-raw",
        type=int,
        default=DEFAULT_LIMIT_MARGIN_RAW,
        help="Warn when a bounded single-turn joint is commanded deeper into a hardware limit by this many raw counts.",
    )
    parser.add_argument(
        "--stiction-eps-deg",
        type=float,
        default=DEFAULT_STICTION_EPS_DEG,
        help="Treat an axis as not moving when measured joint motion stays below this threshold.",
    )
    parser.add_argument(
        "--stiction-frames",
        type=int,
        default=DEFAULT_STICTION_FRAMES,
        help="After this many stagnant command frames, temporarily raise the step to break static friction.",
    )
    parser.add_argument(
        "--pan-breakaway-step",
        type=float,
        default=DEFAULT_PAN_BREAKAWAY_STEP_DEG,
        help="Default pan breakaway step used for both directions unless a directional override is provided.",
    )
    parser.add_argument(
        "--pan-breakaway-step-pos",
        type=float,
        default=None,
        help="Optional pan breakaway step for positive commands.",
    )
    parser.add_argument(
        "--pan-breakaway-step-neg",
        type=float,
        default=DEFAULT_PAN_BREAKAWAY_STEP_NEG_DEG,
        help="Optional pan breakaway step for negative commands. Defaults slightly higher because that side can need extra push.",
    )
    parser.add_argument(
        "--pan-negative-scale",
        type=float,
        default=DEFAULT_PAN_NEGATIVE_SCALE,
        help="Extra scale applied only to negative pan commands to compensate the slower/stickier side.",
    )
    parser.add_argument("--tilt-breakaway-step", type=float, default=DEFAULT_TILT_BREAKAWAY_STEP_DEG)
    parser.add_argument("--dry-run", action="store_true", help="Print intended joint updates without moving hardware.")
    parser.add_argument(
        "--release-torque-on-exit",
        action="store_true",
        help="Disable torque when closing the controller. Default is to keep torque locked.",
    )
    return parser


def _fetch_latest(latest_url: str, timeout_s: float) -> dict[str, Any]:
    opener = _build_url_opener(latest_url)
    with opener.open(latest_url, timeout=max(0.1, float(timeout_s))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected /latest payload type: {type(payload).__name__}")
    return payload


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


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


def _extract_target_center(result: dict[str, Any]) -> tuple[float, float, float, float]:
    payload = result.get("target_center")
    if isinstance(payload, dict):
        try:
            return (
                float(payload["x"]),
                float(payload["y"]),
                float(payload["x_norm"]),
                float(payload["y_norm"]),
            )
        except (KeyError, TypeError, ValueError):
            pass
    x_norm, y_norm = get_target_center_norm()
    frame_size = result.get("frame_size") or [0, 0]
    frame_width = int(frame_size[0]) if len(frame_size) >= 1 else 0
    frame_height = int(frame_size[1]) if len(frame_size) >= 2 else 0
    return (frame_width * x_norm, frame_height * y_norm, x_norm, y_norm)


def _print_hold_reason(frame_id: int, reason: str, *, last_reason: str | None) -> str:
    message = f"[frame {frame_id}] hold: {reason}"
    if message != last_reason:
        print(message, flush=True)
    return message


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


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
        directional_breakaway_abs = abs(float(breakaway_step_pos_deg)) if step_sign > 0 else abs(float(breakaway_step_neg_deg))
        boosted_abs = max(abs(float(step_deg)), directional_breakaway_abs)
        adjusted_step_deg = boosted_abs if step_sign > 0 else -boosted_abs
    elif axis_state.stagnant_frames > 0:
        directional_breakaway_abs = abs(float(breakaway_step_pos_deg)) if step_sign > 0 else abs(float(breakaway_step_neg_deg))
        base_abs = abs(float(step_deg))
        if directional_breakaway_abs > base_abs:
            ramp_ratio = min(1.0, float(axis_state.stagnant_frames) / float(max(1, int(stagnant_frame_threshold))))
            boosted_abs = base_abs + (directional_breakaway_abs - base_abs) * ramp_ratio
            adjusted_step_deg = boosted_abs if step_sign > 0 else -boosted_abs

    axis_state.last_command_sign = step_sign
    axis_state.last_joint_deg = float(current_joint_deg)
    return adjusted_step_deg, measured_delta_deg


def _single_turn_limit_warning(
    controller: SoArmMoceController,
    current_state: dict[str, Any],
    *,
    joint_name: str,
    delta_deg: float,
    limit_margin_raw: int,
) -> str | None:
    if joint_name not in BOUNDED_SINGLE_TURN_JOINTS:
        return None
    calibration_entry = controller._calibration_payload.get(joint_name)
    if not isinstance(calibration_entry, dict):
        return None

    try:
        range_min = int(calibration_entry["range_min"])
        range_max = int(calibration_entry["range_max"])
        present_raw = int(current_state["raw_present_position"][joint_name])
        relative_delta_raw = float(controller._joint_deg_to_relative_raw(joint_name, float(delta_deg)))
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


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    target_x_norm, target_y_norm = get_target_center_norm()
    print(
        (
            "face_follow target center is defined in "
            "Software/Master/face_loc/src/face_tracking/target_center.py "
            f"at ({target_x_norm:.2f}, {target_y_norm:.2f})"
        ),
        flush=True,
    )
    print(
        (
            "If pan/tilt moves the wrong way, adjust --pan-sign / --tilt-sign "
            "or edit the defaults in this script."
        ),
        flush=True,
    )

    controller: SoArmMoceController | None = None
    last_frame_id = -1
    last_hold_reason: str | None = None
    last_limit_warning: str | None = None
    pan_axis_state = AxisFollowState()
    tilt_axis_state = AxisFollowState()
    pan_breakaway_step_pos = (
        float(args.pan_breakaway_step_pos)
        if args.pan_breakaway_step_pos is not None
        else float(args.pan_breakaway_step)
    )
    pan_breakaway_step_neg = (
        float(args.pan_breakaway_step_neg)
        if args.pan_breakaway_step_neg is not None
        else float(args.pan_breakaway_step)
    )
    print(
        (
            f"face_follow breakaway pan(+/-)=({pan_breakaway_step_pos:.2f},{pan_breakaway_step_neg:.2f}) "
            f"tilt=({float(args.tilt_breakaway_step):.2f}) stiction_frames={int(args.stiction_frames)}"
        ),
        flush=True,
    )

    try:
        if not bool(args.dry_run):
            controller = SoArmMoceController(resolve_config(args.config))
            controller._ensure_bus()

        while True:
            try:
                result = _fetch_latest(str(args.latest_url), float(args.http_timeout))
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                last_hold_reason = _print_hold_reason(0, f"failed to fetch tracking result: {exc}", last_reason=last_hold_reason)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            frame_id = int(result.get("frame_id", 0) or 0)
            if frame_id <= 0 or frame_id == last_frame_id:
                time.sleep(max(0.01, float(args.poll_interval)))
                continue
            last_frame_id = frame_id

            if not bool(result.get("detected", False)):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                last_hold_reason = _print_hold_reason(frame_id, f"status={result.get('status', 'unknown')}", last_reason=last_hold_reason)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            offset_payload = result.get("smoothed_offset") or result.get("offset")
            if not isinstance(offset_payload, dict):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                last_hold_reason = _print_hold_reason(frame_id, "tracking result missing offset payload", last_reason=last_hold_reason)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            try:
                ndx = float(offset_payload["ndx"])
                ndy = float(offset_payload["ndy"])
            except (KeyError, TypeError, ValueError):
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                last_hold_reason = _print_hold_reason(frame_id, "tracking result offset payload is invalid", last_reason=last_hold_reason)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            pan_step_deg = _compute_joint_step(
                axis_state=pan_axis_state,
                normalized_offset=ndx,
                gain_deg_per_norm=float(args.pan_gain),
                dead_zone_norm=float(args.pan_dead_zone),
                resume_zone_norm=float(args.pan_resume_zone),
                min_step_deg=float(args.min_pan_step),
                min_step_zone_norm=float(args.pan_min_step_zone),
                max_step_deg=float(args.max_pan_step),
                sign=float(args.pan_sign),
            )
            if pan_step_deg < 0.0:
                negative_scale = max(1.0, float(args.pan_negative_scale))
                pan_step_deg = _clamp(
                    pan_step_deg * negative_scale,
                    -abs(float(args.max_pan_step)) * negative_scale,
                    abs(float(args.max_pan_step)),
                )
            tilt_step_deg = _compute_joint_step(
                axis_state=tilt_axis_state,
                normalized_offset=ndy,
                gain_deg_per_norm=float(args.tilt_gain),
                dead_zone_norm=float(args.tilt_dead_zone),
                resume_zone_norm=float(args.tilt_resume_zone),
                min_step_deg=float(args.min_tilt_step),
                min_step_zone_norm=float(args.tilt_min_step_zone),
                max_step_deg=float(args.max_tilt_step),
                sign=float(args.tilt_sign),
            )

            joint_targets: dict[str, float] = {}
            if abs(pan_step_deg) > 1e-9:
                joint_targets[str(args.pan_joint)] = pan_step_deg
            if abs(tilt_step_deg) > 1e-9:
                tilt_joint_name = str(args.tilt_joint)
                joint_targets[tilt_joint_name] = joint_targets.get(tilt_joint_name, 0.0) + tilt_step_deg

            target_x, target_y, center_x_norm, center_y_norm = _extract_target_center(result)
            target_face = result.get("target_face") if isinstance(result.get("target_face"), dict) else {}
            face_center = target_face.get("center") if isinstance(target_face, dict) else None
            print(
                (
                    f"[frame {frame_id}] target=({target_x:.1f},{target_y:.1f}) "
                    f"target_norm=({center_x_norm:.2f},{center_y_norm:.2f}) "
                    f"face_center={face_center} ndx={ndx:+.3f} ndy={ndy:+.3f} "
                    f"pan_step={pan_step_deg:+.2f} tilt_step={tilt_step_deg:+.2f} "
                    f"pan_active={int(pan_axis_state.active)} tilt_active={int(tilt_axis_state.active)} "
                    f"mode={args.command_mode}"
                ),
                flush=True,
            )

            if not joint_targets:
                last_hold_reason = _print_hold_reason(frame_id, "inside dead zone", last_reason=last_hold_reason)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            last_hold_reason = None

            if bool(args.dry_run):
                print(f"[frame {frame_id}] dry-run move_joints delta_deg={joint_targets}", flush=True)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            assert controller is not None
            try:
                current_state = controller.get_state()
                pan_measured_delta = None
                tilt_measured_delta = None
                pan_joint_name = str(args.pan_joint)
                tilt_joint_name = str(args.tilt_joint)
                if pan_joint_name in joint_targets:
                    pan_step_deg, pan_measured_delta = _apply_stiction_breakaway(
                        pan_axis_state,
                        current_joint_deg=float(current_state["joint_state"][pan_joint_name]),
                        step_deg=float(joint_targets[pan_joint_name]),
                        movement_eps_deg=float(args.stiction_eps_deg),
                        stagnant_frame_threshold=int(args.stiction_frames),
                        breakaway_step_pos_deg=pan_breakaway_step_pos,
                        breakaway_step_neg_deg=pan_breakaway_step_neg,
                    )
                    joint_targets[pan_joint_name] = pan_step_deg
                else:
                    _apply_stiction_breakaway(
                        pan_axis_state,
                        current_joint_deg=float(current_state["joint_state"][pan_joint_name]),
                        step_deg=0.0,
                        movement_eps_deg=float(args.stiction_eps_deg),
                        stagnant_frame_threshold=int(args.stiction_frames),
                        breakaway_step_pos_deg=pan_breakaway_step_pos,
                        breakaway_step_neg_deg=pan_breakaway_step_neg,
                    )
                if tilt_joint_name in joint_targets:
                    tilt_step_deg, tilt_measured_delta = _apply_stiction_breakaway(
                        tilt_axis_state,
                        current_joint_deg=float(current_state["joint_state"][tilt_joint_name]),
                        step_deg=float(joint_targets[tilt_joint_name]),
                        movement_eps_deg=float(args.stiction_eps_deg),
                        stagnant_frame_threshold=int(args.stiction_frames),
                        breakaway_step_pos_deg=float(args.tilt_breakaway_step),
                        breakaway_step_neg_deg=float(args.tilt_breakaway_step),
                    )
                    joint_targets[tilt_joint_name] = tilt_step_deg
                else:
                    _apply_stiction_breakaway(
                        tilt_axis_state,
                        current_joint_deg=float(current_state["joint_state"][tilt_joint_name]),
                        step_deg=0.0,
                        movement_eps_deg=float(args.stiction_eps_deg),
                        stagnant_frame_threshold=int(args.stiction_frames),
                        breakaway_step_pos_deg=float(args.tilt_breakaway_step),
                        breakaway_step_neg_deg=float(args.tilt_breakaway_step),
                    )
                if pan_measured_delta is not None or tilt_measured_delta is not None:
                    print(
                        (
                            f"[frame {frame_id}] motion pan_delta={pan_measured_delta} "
                            f"tilt_delta={tilt_measured_delta} "
                            f"pan_stall={pan_axis_state.stagnant_frames} "
                            f"tilt_stall={tilt_axis_state.stagnant_frames} "
                            f"pan_cmd={joint_targets.get(pan_joint_name, 0.0):+.2f} "
                            f"tilt_cmd={joint_targets.get(tilt_joint_name, 0.0):+.2f}"
                        ),
                        flush=True,
                    )
                limit_warning = None
                for joint_name, delta_deg in joint_targets.items():
                    limit_warning = _single_turn_limit_warning(
                        controller,
                        current_state,
                        joint_name=joint_name,
                        delta_deg=float(delta_deg),
                        limit_margin_raw=int(args.limit_margin_raw),
                    )
                    if limit_warning is not None:
                        break
                if limit_warning is not None:
                    if limit_warning != last_limit_warning:
                        print(f"[frame {frame_id}] warning: {limit_warning}", flush=True)
                    last_limit_warning = limit_warning
                else:
                    last_limit_warning = None
                if str(args.command_mode) == "stream":
                    controller.move_joints(
                        {
                            joint_name: float(current_state["joint_state"][joint_name]) + float(delta_deg)
                            for joint_name, delta_deg in joint_targets.items()
                        },
                        duration=float(args.move_duration),
                        wait=False,
                    )
                else:
                    controller.move_joints(
                        {
                            joint_name: float(current_state["joint_state"][joint_name]) + float(delta_deg)
                            for joint_name, delta_deg in joint_targets.items()
                        },
                        duration=float(args.move_duration),
                        wait=True,
                    )
            except Exception as exc:
                _reset_axis_state(pan_axis_state)
                _reset_axis_state(tilt_axis_state)
                last_hold_reason = _print_hold_reason(frame_id, f"hardware command failed: {type(exc).__name__}: {exc}", last_reason=None)
                time.sleep(max(0.01, float(args.poll_interval)))
                continue

            time.sleep(max(0.01, float(args.poll_interval)))

    except KeyboardInterrupt:
        print("face_follow stopped by user", flush=True)
        return 0
    finally:
        if controller is not None:
            controller.close(disable_torque=bool(args.release_torque_on_exit))


if __name__ == "__main__":
    raise SystemExit(main())
