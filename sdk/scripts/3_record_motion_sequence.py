#!/usr/bin/env python3
"""Record a continuous hand-guided joint trajectory to JSON."""

from __future__ import annotations

import argparse
import select
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from _motion_sequence_common import (
    DEFAULT_SEQUENCE_SAVE_PATH,
    build_sample_from_state,
    lock_current_pose,
    make_controller,
    resolve_save_path,
    save_sequence_file,
    sequence_relpath,
)
from soarmmoce_sdk import ValidationError
from soarmmoce_sdk.cli_common import cli_bool, print_error, print_success


def _open_interactive_input() -> tuple[TextIO | None, bool]:
    stream = sys.stdin
    if stream is not None and not getattr(stream, "closed", False) and stream.isatty():
        return stream, False
    try:
        return open("/dev/tty", "r", encoding="utf-8", errors="ignore"), True
    except Exception:
        return None, False


def _enter_pressed(stream: TextIO | None) -> bool:
    if stream is None:
        return False
    ready, _, _ = select.select([stream.fileno()], [], [], 0.0)
    if not ready:
        return False
    stream.readline()
    return True


def record_motion_sequence(
    *,
    save_path: str | Path | None,
    timestamped: bool,
    sample_rate_hz: float,
    duration_sec: float,
    max_samples: int,
    disable_torque: bool,
    lock_on_exit: bool,
    release_torque_on_exit: bool,
    config_path: str | Path | None,
    controller: Any | None = None,
    state_provider: Callable[[], Any] | None = None,
    before_record_callback: Callable[[], Any] | None = None,
    lock_on_exit_callback: Callable[[], Any] | None = None,
    stop_event: Any | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    interactive: bool = True,
    close_controller: bool | None = None,
) -> dict[str, object]:
    sample_rate = float(sample_rate_hz)
    if sample_rate <= 0.0:
        raise ValidationError("--sample-rate-hz must be > 0")
    duration = max(0.0, float(duration_sec))
    max_sample_count = max(0, int(max_samples))
    resolved_save_path = resolve_save_path(save_path, timestamped=bool(timestamped))

    input_stream: TextIO | None = None
    close_input_stream = False
    if duration <= 0.0 and max_sample_count <= 0 and stop_event is None and interactive:
        input_stream, close_input_stream = _open_interactive_input()
        if input_stream is None:
            raise ValidationError(
                "非交互式终端里需要设置 --duration-sec 或 --max-samples；"
                "交互式终端可直接按 Enter 停止录制。"
            )
    elif duration <= 0.0 and max_sample_count <= 0 and stop_event is None:
        raise ValidationError("函数调用模式需要提供 stop_event、--duration-sec 或 --max-samples")

    arm = controller if controller is not None else (None if state_provider is not None else make_controller(config_path))
    should_close_controller = bool(close_controller) if close_controller is not None else arm is not None and controller is None
    samples: list[dict[str, object]] = []
    locked_hold_state: dict[str, object] | None = None
    try:
        if arm is not None:
            arm._ensure_bus()  # noqa: SLF001 - fail early before changing torque state.
        if disable_torque:
            if before_record_callback is not None:
                before_record_callback()
            elif arm is not None:
                arm.disable_torque()
                arm.set_manual_multi_turn_readback(True)
            print(
                "[record-motion] 力矩已解锁，可以手动拖动机械臂；录制结束会锁住当前位置。",
                file=sys.stderr,
                flush=True,
            )

        if duration > 0.0:
            print(
                f"[record-motion] 开始连续录制 {duration:.2f}s，采样率 {sample_rate:.1f} Hz -> "
                f"{sequence_relpath(resolved_save_path)}",
                file=sys.stderr,
                flush=True,
            )
        elif max_sample_count > 0:
            print(
                f"[record-motion] 开始连续录制 {max_sample_count} 个样本，采样率 {sample_rate:.1f} Hz -> "
                f"{sequence_relpath(resolved_save_path)}",
                file=sys.stderr,
                flush=True,
            )
        else:
            stop_hint = "等待 GUI Stop 停止" if stop_event is not None else "按 Enter 停止"
            print(
                f"[record-motion] 开始连续录制，{stop_hint}；采样率 {sample_rate:.1f} Hz -> "
                f"{sequence_relpath(resolved_save_path)}",
                file=sys.stderr,
                flush=True,
            )

        interval = 1.0 / sample_rate
        started_monotonic = time.monotonic()
        next_sample_at = started_monotonic
        while True:
            if stop_event is not None and bool(stop_event.is_set()) and samples:
                break
            now = time.monotonic()
            if now < next_sample_at:
                time.sleep(max(0.0, next_sample_at - now))

            if state_provider is not None:
                state = state_provider()
            elif arm is not None:
                state = arm.get_state()
            else:
                raise ValidationError("record_motion_sequence requires a controller or state_provider")
            sample = build_sample_from_state(
                state,
                index=len(samples) + 1,
                started_monotonic=started_monotonic,
            )
            samples.append(sample)

            elapsed = float(sample.get("t", 0.0) or 0.0)
            if len(samples) == 1 or len(samples) % max(1, int(round(sample_rate))) == 0:
                print(
                    f"[record-motion] samples={len(samples)} elapsed={elapsed:.2f}s",
                    file=sys.stderr,
                    flush=True,
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "sample_count": len(samples),
                            "duration_sec": elapsed,
                            "sample_rate_hz": sample_rate,
                            "save_path": str(resolved_save_path),
                            "sample": sample,
                        }
                    )

            if duration > 0.0 and elapsed >= duration:
                break
            if max_sample_count > 0 and len(samples) >= max_sample_count:
                break
            if duration <= 0.0 and max_sample_count <= 0 and _enter_pressed(input_stream):
                break

            next_sample_at += interval
            behind_by = time.monotonic() - next_sample_at
            if behind_by > interval:
                next_sample_at = time.monotonic() + interval

    except KeyboardInterrupt:
        print("[record-motion] 收到 Ctrl-C，停止并保存已录制样本。", file=sys.stderr, flush=True)
    finally:
        if close_input_stream and input_stream is not None:
            try:
                input_stream.close()
            except Exception:
                pass
        if lock_on_exit:
            try:
                if lock_on_exit_callback is not None:
                    locked_hold_state = lock_on_exit_callback()
                elif arm is not None:
                    locked_hold_state = lock_current_pose(arm)
            except Exception as exc:
                print(f"[record-motion][warn] 结束时未能锁住当前位置: {exc}", file=sys.stderr, flush=True)
        if should_close_controller and arm is not None:
            arm.close(disable_torque=bool(release_torque_on_exit))

    saved_path = save_sequence_file(resolved_save_path, samples=samples, sample_rate_hz=sample_rate)
    return {
        "action": "record_motion_sequence",
        "saved_path": str(saved_path),
        "sample_count": len(samples),
        "duration_sec": float(samples[-1].get("t", 0.0) or 0.0) if samples else 0.0,
        "sample_rate_hz": sample_rate,
        "disable_torque": bool(disable_torque),
        "lock_on_exit": bool(lock_on_exit),
        "locked_hold_state": locked_hold_state,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a continuous joint trajectory JSON file")
    parser.add_argument(
        "--save-path",
        default=str(DEFAULT_SEQUENCE_SAVE_PATH),
        help="JSON 文件路径；如果传目录，会自动生成 recorded_motion_sequence_时间戳.json",
    )
    parser.add_argument("--timestamped", type=cli_bool, default=False, help="给文件名追加时间戳，避免覆盖")
    parser.add_argument("--sample-rate-hz", type=float, default=50.0)
    parser.add_argument("--duration-sec", type=float, default=0.0, help=">0 时录制固定秒数；默认按 Enter 停止")
    parser.add_argument("--max-samples", type=int, default=0, help=">0 时达到样本数后停止")
    parser.add_argument("--disable-torque", type=cli_bool, default=True, help="录制时释放力矩，方便手动拖动")
    parser.add_argument("--lock-on-exit", type=cli_bool, default=True, help="结束时重新锁住当前位置")
    parser.add_argument("--release-torque-on-exit", type=cli_bool, default=False)
    parser.add_argument("--config-path", default=None)
    args = parser.parse_args()

    try:
        print_success(
            record_motion_sequence(
                save_path=args.save_path,
                timestamped=bool(args.timestamped),
                sample_rate_hz=float(args.sample_rate_hz),
                duration_sec=float(args.duration_sec),
                max_samples=int(args.max_samples),
                disable_torque=bool(args.disable_torque),
                lock_on_exit=bool(args.lock_on_exit),
                release_torque_on_exit=bool(args.release_torque_on_exit),
                config_path=args.config_path,
            )
        )
    except Exception as exc:
        print_error(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
