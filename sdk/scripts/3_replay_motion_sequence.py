#!/usr/bin/env python3
"""Replay a continuous joint trajectory JSON file."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from _motion_sequence_common import (
    DEFAULT_SEQUENCE_SAVE_PATH,
    gripper_raw_from_sample,
    load_sequence_file,
    lock_current_pose,
    make_controller,
    replay_sample_duration,
    sample_time,
    sequence_relpath,
    targets_from_sample,
)
from soarmmoce_sdk import JOINTS, MULTI_TURN_JOINTS, ValidationError, to_jsonable
from soarmmoce_sdk.cli_common import cli_bool, print_error, print_success


def _raw_goal_from_targets(
    arm: Any,
    targets_deg: dict[str, float],
    multi_turn_targets: dict[str, float],
) -> dict[str, int]:
    goal_raw: dict[str, int] = {}
    for joint_name in JOINTS:
        if joint_name in multi_turn_targets:
            goal_raw[joint_name] = int(
                arm._continuous_raw_to_multi_turn_goal_raw(joint_name, float(multi_turn_targets[joint_name]))
            )
        elif joint_name in targets_deg:
            goal_raw[joint_name] = int(arm._joint_deg_to_absolute_goal_raw(joint_name, float(targets_deg[joint_name])))
    return goal_raw


def _write_goal_raw(arm: Any, bus: Any, goal_raw: dict[str, int]) -> None:
    if not goal_raw:
        return
    arm._write_raw_goal_positions(bus, goal_raw)
    for joint_name, raw_value in goal_raw.items():
        if joint_name in MULTI_TURN_JOINTS:
            arm._last_multi_turn_goal_raw_mod[joint_name] = int(raw_value)


def _interpolate_scalar(a: float, b: float, alpha: float) -> float:
    alpha_value = max(0.0, min(1.0, float(alpha)))
    return float(a) + (float(b) - float(a)) * alpha_value


def _interpolate_targets(
    first: dict[str, Any],
    second: dict[str, Any],
    alpha: float,
) -> tuple[dict[str, float], dict[str, float]]:
    first_targets = dict(first["targets_deg"])
    second_targets = dict(second["targets_deg"])
    first_multi = dict(first["multi_turn_targets"])
    second_multi = dict(second["multi_turn_targets"])

    targets_deg: dict[str, float] = {}
    multi_turn_targets: dict[str, float] = {}
    for joint_name in JOINTS:
        if joint_name in first_targets and joint_name in second_targets:
            targets_deg[joint_name] = _interpolate_scalar(first_targets[joint_name], second_targets[joint_name], alpha)
        elif joint_name in second_targets:
            targets_deg[joint_name] = float(second_targets[joint_name])
        elif joint_name in first_targets:
            targets_deg[joint_name] = float(first_targets[joint_name])

        if joint_name in MULTI_TURN_JOINTS:
            if joint_name in first_multi and joint_name in second_multi:
                multi_turn_targets[joint_name] = _interpolate_scalar(first_multi[joint_name], second_multi[joint_name], alpha)
            elif joint_name in second_multi:
                multi_turn_targets[joint_name] = float(second_multi[joint_name])
            elif joint_name in first_multi:
                multi_turn_targets[joint_name] = float(first_multi[joint_name])

    return targets_deg, multi_turn_targets


def _interpolate_gripper_raw(first: dict[str, Any], second: dict[str, Any], alpha: float) -> int | None:
    first_raw = first.get("gripper_raw")
    second_raw = second.get("gripper_raw")
    if isinstance(first_raw, (int, float)) and isinstance(second_raw, (int, float)):
        return int(round(_interpolate_scalar(float(first_raw), float(second_raw), alpha)))
    if isinstance(second_raw, (int, float)):
        return int(second_raw)
    if isinstance(first_raw, (int, float)):
        return int(first_raw)
    return None


def _raw_targets_from_sample(sample: Mapping[str, Any]) -> dict[str, int]:
    raw_present = sample.get("raw_present_position")
    if not isinstance(raw_present, Mapping):
        return {}
    return {
        joint_name: int(raw_present[joint_name])
        for joint_name in JOINTS
        if isinstance(raw_present.get(joint_name), (int, float))
    }


def _interpolate_raw_targets(first: dict[str, Any], second: dict[str, Any], alpha: float) -> dict[str, int]:
    first_raw = dict(first.get("raw_targets") or {})
    second_raw = dict(second.get("raw_targets") or {})
    raw_targets: dict[str, int] = {}
    for joint_name in JOINTS:
        if joint_name in first_raw and joint_name in second_raw:
            raw_targets[joint_name] = int(
                round(_interpolate_scalar(float(first_raw[joint_name]), float(second_raw[joint_name]), alpha))
            )
        elif joint_name in second_raw:
            raw_targets[joint_name] = int(second_raw[joint_name])
        elif joint_name in first_raw:
            raw_targets[joint_name] = int(first_raw[joint_name])
    return raw_targets


def _gripper_ratio_from_sample(sample: Mapping[str, Any]) -> float | None:
    for key in ("gripper_state", "gripper"):
        payload = sample.get(key)
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("open_ratio")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return None


def _interpolate_gripper_ratio(first: dict[str, Any], second: dict[str, Any], alpha: float) -> float | None:
    first_ratio = first.get("gripper_ratio")
    second_ratio = second.get("gripper_ratio")
    if isinstance(first_ratio, (int, float)) and isinstance(second_ratio, (int, float)):
        return max(0.0, min(1.0, _interpolate_scalar(float(first_ratio), float(second_ratio), alpha)))
    if isinstance(second_ratio, (int, float)):
        return max(0.0, min(1.0, float(second_ratio)))
    if isinstance(first_ratio, (int, float)):
        return max(0.0, min(1.0, float(first_ratio)))
    return None


def _replay_items(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for sample in samples:
        targets_deg, multi_turn_targets = targets_from_sample(sample)
        if not targets_deg:
            continue
        items.append(
            {
                "t": sample_time(sample),
                "targets_deg": targets_deg,
                "multi_turn_targets": multi_turn_targets,
                "raw_targets": _raw_targets_from_sample(sample),
                "gripper_raw": gripper_raw_from_sample(sample),
                "gripper_ratio": _gripper_ratio_from_sample(sample),
            }
        )
    return items


def _move_to_first_item(
    *,
    arm: Any,
    item: dict[str, Any],
    duration: float,
    speed_percent: int,
    timeout_margin_sec: float,
) -> Any:
    move_kwargs: dict[str, Any] = {
        "duration": max(0.0, float(duration)),
        "speed_percent": int(speed_percent),
        "wait": True,
        "timeout": max(1.0, float(duration) + float(timeout_margin_sec)),
    }
    if item["multi_turn_targets"]:
        move_kwargs["multi_turn_targets_continuous_raw"] = dict(item["multi_turn_targets"])
    return arm.move_joints(dict(item["targets_deg"]), **move_kwargs)


def _stream_replay_items(
    *,
    arm: Any,
    items: list[dict[str, Any]],
    first_duration: float,
    speed: float,
    speed_percent: int,
    stream_hz: float,
    wait_timeout_margin_sec: float,
    replay_gripper: bool,
    final_settle: bool,
    stop_event: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    bus = arm._ensure_bus()
    first_result = _move_to_first_item(
        arm=arm,
        item=items[0],
        duration=float(first_duration),
        speed_percent=int(speed_percent),
        timeout_margin_sec=float(wait_timeout_margin_sec),
    )
    if replay_gripper and isinstance(items[0].get("gripper_raw"), (int, float)):
        arm.write_gripper_raw(int(items[0]["gripper_raw"]), bus=bus)

    if len(items) == 1:
        return {
            "played_count": 1,
            "stream_tick_count": 0,
            "final_goal_raw": _raw_goal_from_targets(arm, items[0]["targets_deg"], items[0]["multi_turn_targets"]),
            "final_wait": None,
            "final_state": first_result.get("state") if isinstance(first_result, dict) else arm.get_state(),
        }

    source_start = float(items[0]["t"])
    source_end = max(source_start, float(items[-1]["t"]))
    total_source_duration = max(0.0, source_end - source_start)
    tick_hz = max(1.0, float(stream_hz))
    tick_period = 1.0 / tick_hz
    speed_value = max(0.1, float(speed))
    stream_started_at = time.monotonic()
    segment_index = 0
    tick_index = 1
    stream_tick_count = 0
    last_goal_raw: dict[str, int] = {}
    last_gripper_raw: int | None = int(items[0]["gripper_raw"]) if isinstance(items[0].get("gripper_raw"), (int, float)) else None

    while True:
        if stop_event is not None and bool(stop_event.is_set()):
            break
        real_elapsed = float(tick_index) * tick_period
        source_elapsed = real_elapsed * speed_value
        if source_elapsed >= total_source_duration:
            break

        target_wall_time = stream_started_at + real_elapsed
        sleep_sec = target_wall_time - time.monotonic()
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
        if stop_event is not None and bool(stop_event.is_set()):
            break

        current_source_time = source_start + source_elapsed
        while segment_index < len(items) - 2 and float(items[segment_index + 1]["t"]) < current_source_time:
            segment_index += 1

        first = items[segment_index]
        second = items[segment_index + 1]
        span = max(1e-6, float(second["t"]) - float(first["t"]))
        alpha = (current_source_time - float(first["t"])) / span
        targets_deg, multi_turn_targets = _interpolate_targets(first, second, alpha)
        last_goal_raw = _raw_goal_from_targets(arm, targets_deg, multi_turn_targets)
        _write_goal_raw(arm, bus, last_goal_raw)

        if replay_gripper:
            gripper_raw = _interpolate_gripper_raw(first, second, alpha)
            if gripper_raw is not None and gripper_raw != last_gripper_raw:
                arm.write_gripper_raw(int(gripper_raw), bus=bus)
                last_gripper_raw = int(gripper_raw)

        stream_tick_count += 1
        if progress_callback is not None and (
            stream_tick_count == 1 or stream_tick_count % max(1, int(round(tick_hz / 2.0))) == 0
        ):
            progress_callback(
                {
                    "played_count": len(items),
                    "stream_tick_count": int(stream_tick_count),
                    "source_time_sec": float(current_source_time),
                    "stream_hz": float(stream_hz),
                }
            )
        tick_index += 1

    if stop_event is not None and bool(stop_event.is_set()):
        return {
            "played_count": len(items),
            "stream_tick_count": int(stream_tick_count),
            "final_goal_raw": last_goal_raw,
            "final_wait": None,
            "final_state": arm.get_state(),
            "cancelled": True,
        }

    final_item = items[-1]
    final_goal_raw = _raw_goal_from_targets(arm, final_item["targets_deg"], final_item["multi_turn_targets"])
    _write_goal_raw(arm, bus, final_goal_raw)
    if replay_gripper and isinstance(final_item.get("gripper_raw"), (int, float)):
        arm.write_gripper_raw(int(final_item["gripper_raw"]), bus=bus)

    final_wait = None
    if final_settle:
        final_wait = arm._wait_for_motion(
            bus,
            final_goal_raw,
            duration=0.0,
            timeout=max(0.2, float(wait_timeout_margin_sec)),
        )

    return {
        "played_count": len(items),
        "stream_tick_count": int(stream_tick_count),
        "final_goal_raw": final_goal_raw,
        "final_wait": to_jsonable(final_wait),
        "final_state": arm.get_state(),
    }


def _stream_replay_with_sink(
    *,
    command_sink: Callable[[dict[str, Any]], Any],
    items: list[dict[str, Any]],
    first_duration: float,
    speed: float,
    speed_percent: int,
    stream_hz: float,
    replay_gripper: bool,
    stop_event: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    tick_hz = max(1.0, float(stream_hz))
    tick_period = 1.0 / tick_hz
    speed_value = max(0.1, float(speed))
    stream_tick_count = 0
    last_result: Any = None

    def emit_item(item: dict[str, Any], *, duration_sec: float, tick_index: int) -> Any:
        payload: dict[str, Any] = {
            "targets_deg": dict(item.get("targets_deg") or {}),
            "multi_turn_targets": dict(item.get("multi_turn_targets") or {}),
            "raw_targets": dict(item.get("raw_targets") or {}),
            "duration_sec": float(duration_sec),
            "speed_percent": int(speed_percent),
            "tick_index": int(tick_index),
        }
        if replay_gripper:
            if isinstance(item.get("gripper_raw"), (int, float)):
                payload["gripper_raw"] = int(item["gripper_raw"])
            if isinstance(item.get("gripper_ratio"), (int, float)):
                payload["gripper_ratio"] = float(item["gripper_ratio"])
        return command_sink(payload)

    last_result = emit_item(items[0], duration_sec=float(first_duration), tick_index=0)
    if len(items) == 1 or (stop_event is not None and bool(stop_event.is_set())):
        return {
            "played_count": 1,
            "stream_tick_count": 0,
            "final_goal_raw": dict(items[0].get("raw_targets") or {}),
            "final_wait": None,
            "final_state": last_result,
            "cancelled": bool(stop_event is not None and stop_event.is_set()),
        }

    source_start = float(items[0]["t"])
    source_end = max(source_start, float(items[-1]["t"]))
    total_source_duration = max(0.0, source_end - source_start)
    stream_started_at = time.monotonic()
    segment_index = 0
    tick_index = 1

    while True:
        if stop_event is not None and bool(stop_event.is_set()):
            break
        real_elapsed = float(tick_index) * tick_period
        source_elapsed = real_elapsed * speed_value
        if source_elapsed >= total_source_duration:
            break

        target_wall_time = stream_started_at + real_elapsed
        sleep_sec = target_wall_time - time.monotonic()
        if sleep_sec > 0.0:
            time.sleep(sleep_sec)
        if stop_event is not None and bool(stop_event.is_set()):
            break

        current_source_time = source_start + source_elapsed
        while segment_index < len(items) - 2 and float(items[segment_index + 1]["t"]) < current_source_time:
            segment_index += 1

        first = items[segment_index]
        second = items[segment_index + 1]
        span = max(1e-6, float(second["t"]) - float(first["t"]))
        alpha = (current_source_time - float(first["t"])) / span
        targets_deg, multi_turn_targets = _interpolate_targets(first, second, alpha)
        command_item = {
            "t": float(current_source_time),
            "targets_deg": targets_deg,
            "multi_turn_targets": multi_turn_targets,
            "raw_targets": _interpolate_raw_targets(first, second, alpha),
            "gripper_raw": _interpolate_gripper_raw(first, second, alpha),
            "gripper_ratio": _interpolate_gripper_ratio(first, second, alpha),
        }
        last_result = emit_item(command_item, duration_sec=tick_period, tick_index=tick_index)
        stream_tick_count += 1
        if progress_callback is not None and (
            stream_tick_count == 1 or stream_tick_count % max(1, int(round(tick_hz / 2.0))) == 0
        ):
            progress_callback(
                {
                    "played_count": len(items),
                    "stream_tick_count": int(stream_tick_count),
                    "source_time_sec": float(current_source_time),
                    "stream_hz": float(stream_hz),
                }
            )
        tick_index += 1

    cancelled = bool(stop_event is not None and stop_event.is_set())
    final_item = items[-1]
    if not cancelled:
        last_result = emit_item(final_item, duration_sec=tick_period, tick_index=tick_index)

    return {
        "played_count": len(items),
        "stream_tick_count": int(stream_tick_count),
        "final_goal_raw": dict(final_item.get("raw_targets") or {}),
        "final_wait": None,
        "final_state": last_result,
        "cancelled": cancelled,
    }


def _step_replay_items(
    *,
    arm: Any,
    samples: list[dict[str, Any]],
    durations: list[float],
    speed_percent: int,
    wait_timeout_margin_sec: float,
    replay_gripper: bool,
    stop_event: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    final_state = None
    played_count = 0
    for index, sample in enumerate(samples):
        if stop_event is not None and bool(stop_event.is_set()):
            break
        targets_deg, multi_turn_targets = targets_from_sample(sample)
        if not targets_deg:
            continue
        duration = float(durations[index])
        if replay_gripper:
            gripper_raw = gripper_raw_from_sample(sample)
            if gripper_raw is not None:
                arm.write_gripper_raw(int(gripper_raw))

        move_kwargs: dict[str, Any] = {
            "duration": duration,
            "speed_percent": int(speed_percent),
            "wait": True,
            "timeout": max(1.0, duration + float(wait_timeout_margin_sec)),
        }
        if multi_turn_targets:
            move_kwargs["multi_turn_targets_continuous_raw"] = multi_turn_targets
        move_result = arm.move_joints(targets_deg, **move_kwargs)
        final_state = move_result.get("state") if isinstance(move_result, dict) else arm.get_state()
        played_count += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "played_count": int(played_count),
                    "sample_count": len(samples),
                    "stream_tick_count": 0,
                }
            )

    return {
        "played_count": played_count,
        "stream_tick_count": 0,
        "final_goal_raw": {},
        "final_wait": None,
        "final_state": final_state if final_state is not None else arm.get_state(),
        "cancelled": bool(stop_event is not None and stop_event.is_set()),
    }


def replay_motion_sequence(
    *,
    replay_path: str | Path | None,
    speed: float,
    speed_percent: int,
    replay_mode: str,
    stream_hz: float,
    initial_duration_sec: float | None,
    min_step_duration_sec: float,
    max_step_duration_sec: float,
    wait_timeout_margin_sec: float,
    replay_gripper: bool,
    final_settle: bool,
    dry_run: bool,
    release_torque_on_exit: bool,
    config_path: str | Path | None,
    controller: Any | None = None,
    command_sink: Callable[[dict[str, Any]], Any] | None = None,
    stop_event: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    close_controller: bool | None = None,
) -> dict[str, object]:
    if float(speed) <= 0.0:
        raise ValidationError("--speed must be > 0")
    mode = str(replay_mode or "stream").strip().lower()
    if mode not in {"stream", "step"}:
        raise ValidationError("--replay-mode must be 'stream' or 'step'")
    if float(stream_hz) <= 0.0:
        raise ValidationError("--stream-hz must be > 0")

    resolved_path, payload, samples = load_sequence_file(replay_path)
    durations = [
        replay_sample_duration(
            samples,
            index,
            speed=float(speed),
            initial_duration_sec=initial_duration_sec,
            min_step_duration_sec=float(min_step_duration_sec),
            max_step_duration_sec=float(max_step_duration_sec),
        )
        for index in range(len(samples))
    ]

    items = _replay_items(samples)
    replayable_count = len(items)
    first_targets = dict(items[0]["targets_deg"]) if items else {}
    last_targets = dict(items[-1]["targets_deg"]) if items else {}

    if replayable_count <= 0:
        raise ValidationError(f"录制文件里没有可回放的关节目标: {resolved_path}")

    if dry_run:
        source_start = float(items[0]["t"])
        source_end = float(items[-1]["t"])
        source_duration = max(0.0, source_end - source_start)
        estimated_stream_ticks = int(source_duration / max(1e-6, float(speed)) * max(1.0, float(stream_hz)))
        return {
            "action": "replay_motion_sequence",
            "dry_run": True,
            "replay_path": str(resolved_path),
            "replay_mode": mode,
            "format": payload.get("format"),
            "sample_count": len(samples),
            "replayable_count": replayable_count,
            "source_duration_sec": payload.get("duration_sec"),
            "estimated_replay_duration_sec": sum(durations),
            "estimated_stream_ticks": estimated_stream_ticks if mode == "stream" else 0,
            "stream_hz": float(stream_hz) if mode == "stream" else None,
            "speed": float(speed),
            "first_targets_deg": first_targets,
            "last_targets_deg": last_targets,
        }

    if command_sink is not None:
        if mode != "stream":
            raise ValidationError("函数调用 command_sink 模式只支持 --replay-mode stream")
        first_duration = float(durations[0]) if initial_duration_sec is None else float(initial_duration_sec)
        replay_summary = _stream_replay_with_sink(
            command_sink=command_sink,
            items=items,
            first_duration=first_duration,
            speed=float(speed),
            speed_percent=int(speed_percent),
            stream_hz=float(stream_hz),
            replay_gripper=bool(replay_gripper),
            stop_event=stop_event,
            progress_callback=progress_callback,
        )
        return {
            "action": "replay_motion_sequence",
            "replay_path": str(resolved_path),
            "replay_mode": mode,
            "sample_count": len(samples),
            "played_count": int(replay_summary["played_count"]),
            "stream_hz": float(stream_hz),
            "stream_tick_count": int(replay_summary.get("stream_tick_count") or 0),
            "speed": float(speed),
            "speed_percent": int(speed_percent),
            "cancelled": bool(replay_summary.get("cancelled", False)),
            "final_wait": replay_summary.get("final_wait"),
            "final_state": to_jsonable(replay_summary.get("final_state")),
        }

    arm = controller if controller is not None else make_controller(config_path)
    should_close_controller = bool(close_controller) if close_controller is not None else controller is None
    try:
        lock_current_pose(arm)
        print(
            f"[replay-motion] 开始回放 {len(samples)} 个样本: {sequence_relpath(resolved_path)} "
            f"mode={mode} speed={float(speed):.2f}x speed_percent={int(speed_percent)}",
            file=sys.stderr,
            flush=True,
        )

        if mode == "stream":
            first_duration = float(durations[0]) if initial_duration_sec is None else float(initial_duration_sec)
            replay_summary = _stream_replay_items(
                arm=arm,
                items=items,
                first_duration=first_duration,
                speed=float(speed),
                speed_percent=int(speed_percent),
                stream_hz=float(stream_hz),
                wait_timeout_margin_sec=float(wait_timeout_margin_sec),
                replay_gripper=bool(replay_gripper),
                final_settle=bool(final_settle),
                stop_event=stop_event,
                progress_callback=progress_callback,
            )
        else:
            replay_summary = _step_replay_items(
                arm=arm,
                samples=samples,
                durations=durations,
                speed_percent=int(speed_percent),
                wait_timeout_margin_sec=float(wait_timeout_margin_sec),
                replay_gripper=bool(replay_gripper),
                stop_event=stop_event,
                progress_callback=progress_callback,
            )

        return {
            "action": "replay_motion_sequence",
            "replay_path": str(resolved_path),
            "replay_mode": mode,
            "sample_count": len(samples),
            "played_count": int(replay_summary["played_count"]),
            "stream_hz": float(stream_hz) if mode == "stream" else None,
            "stream_tick_count": int(replay_summary.get("stream_tick_count") or 0),
            "speed": float(speed),
            "speed_percent": int(speed_percent),
            "cancelled": bool(replay_summary.get("cancelled", False)),
            "final_wait": replay_summary.get("final_wait"),
            "final_state": to_jsonable(replay_summary["final_state"]),
        }
    finally:
        if should_close_controller:
            arm.close(disable_torque=bool(release_torque_on_exit))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a recorded continuous joint trajectory JSON file")
    parser.add_argument("path", nargs="?", default="", help="录制 JSON 文件；也可以传目录")
    parser.add_argument("--replay-path", default="", help="录制 JSON 文件；优先级高于位置参数")
    parser.add_argument("--speed", type=float, default=1.0, help="时间缩放，2.0 表示两倍速")
    parser.add_argument("--speed-percent", type=int, default=30)
    parser.add_argument("--replay-mode", choices=("stream", "step"), default="stream", help="stream 更丝滑；step 是旧的逐点等待模式")
    parser.add_argument("--stream-hz", type=float, default=50.0, help="stream 模式下的目标下发频率")
    parser.add_argument(
        "--initial-duration-sec",
        type=float,
        default=None,
        help="移动到第一个样本的时长；不设置时自动估算",
    )
    parser.add_argument("--min-step-duration-sec", type=float, default=0.03)
    parser.add_argument("--max-step-duration-sec", type=float, default=2.0)
    parser.add_argument("--wait-timeout-margin-sec", type=float, default=2.0)
    parser.add_argument("--replay-gripper", type=cli_bool, default=True)
    parser.add_argument("--final-settle", type=cli_bool, default=True, help="stream 结束后等待最终姿态到位")
    parser.add_argument("--dry-run", type=cli_bool, default=False, help="只校验文件并估算时长，不连接机械臂")
    parser.add_argument("--release-torque-on-exit", type=cli_bool, default=False)
    parser.add_argument("--config-path", default=None)
    args = parser.parse_args()

    path = args.replay_path or args.path or str(DEFAULT_SEQUENCE_SAVE_PATH)
    try:
        print_success(
            replay_motion_sequence(
                replay_path=path,
                speed=float(args.speed),
                speed_percent=int(args.speed_percent),
                replay_mode=str(args.replay_mode),
                stream_hz=float(args.stream_hz),
                initial_duration_sec=args.initial_duration_sec,
                min_step_duration_sec=float(args.min_step_duration_sec),
                max_step_duration_sec=float(args.max_step_duration_sec),
                wait_timeout_margin_sec=float(args.wait_timeout_margin_sec),
                replay_gripper=bool(args.replay_gripper),
                final_settle=bool(args.final_settle),
                dry_run=bool(args.dry_run),
                release_torque_on_exit=bool(args.release_torque_on_exit),
                config_path=args.config_path,
            )
        )
    except Exception as exc:
        print_error(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
