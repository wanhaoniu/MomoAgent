#!/usr/bin/env python3
"""Record multiple poses, return to pose 1, then replay them in order."""

from __future__ import annotations

import argparse
import json
import re
import select
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from soarmmoce_cli_common import cli_bool, print_error, print_success
from soarmmoce_sdk import JOINTS, MULTI_TURN_JOINTS, SoArmMoceController, ValidationError, to_jsonable


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAVE_PATH = SKILL_ROOT / "workspace" / "runtime" / "recorded_pose_sequence.json"
DEFAULT_POSE_COUNT = 2
AUTO_FLIP_REPLAY_JOINTS = {"elbow_flex"}
LIMIT_ERROR_LINE_RE = re.compile(
    r"^(?P<joint>[A-Za-z0-9_]+): target=(?P<target>[+-]?\d+(?:\.\d+)?) deg"
    r"(?:, current=(?P<current>[+-]?\d+(?:\.\d+)?) deg)?"
    r", allowed=\[(?P<min>[+-]?\d+(?:\.\d+)?), (?P<max>[+-]?\d+(?:\.\d+)?)\] deg$"
)


def _wait_for_enter(
    prompt: str,
    *,
    arm: SoArmMoceController | None = None,
    poll_interval_sec: float = 0.05,
) -> None:
    print(prompt, file=sys.stderr, flush=True)

    stream = sys.stdin
    close_stream = False
    try:
        if stream is None or getattr(stream, "closed", False) or not stream.isatty():
            stream = open("/dev/tty", "r", encoding="utf-8", errors="ignore")
            close_stream = True

        fileno = stream.fileno()
        while True:
            ready, _, _ = select.select([fileno], [], [], max(0.01, float(poll_interval_sec)))
            if ready:
                line = stream.readline()
                if line == "":
                    raise RuntimeError(
                        "无法从终端读取 Enter；请在交互式终端里运行，或加 "
                        "--wait-for-record-enter false / --wait-between-poses false"
                    )
                return
            if arm is not None:
                try:
                    arm.get_state()
                except Exception:
                    pass
    finally:
        if close_stream and stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _prompt_pose_count(*, default: int = DEFAULT_POSE_COUNT) -> int:
    default = max(1, int(default))
    stream = sys.stdin
    close_stream = False
    try:
        if stream is None or getattr(stream, "closed", False) or not stream.isatty():
            try:
                stream = open("/dev/tty", "r", encoding="utf-8", errors="ignore")
                close_stream = True
            except Exception:
                return default

        while True:
            print(
                f"[record-pose] 请输入要录制的姿态数量，直接回车默认 {default}: ",
                file=sys.stderr,
                flush=True,
                end="",
            )
            line = stream.readline()
            if line == "":
                return default
            raw = line.strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                print("[record-pose][warn] 请输入一个正整数。", file=sys.stderr, flush=True)
                continue
            if value <= 0:
                print("[record-pose][warn] 姿态数量必须大于 0。", file=sys.stderr, flush=True)
                continue
            return int(value)
    finally:
        if close_stream and stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _record_joint_targets(state: Dict[str, Any]) -> Dict[str, float]:
    joint_state = state.get("joint_state")
    if not isinstance(joint_state, dict):
        raise ValidationError("Robot state is missing joint_state")
    targets: Dict[str, float] = {}
    for joint_name in JOINTS:
        if joint_name not in joint_state:
            raise ValidationError(f"Robot state is missing joint: {joint_name}")
        targets[joint_name] = float(joint_state[joint_name])
    return targets


def _save_recorded_poses(path: Path, *, recorded_poses: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pose_count": len(recorded_poses),
        "poses": to_jsonable(recorded_poses),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _limit_violations(
    *,
    joint_targets: Dict[str, float],
    joint_limits_deg: Dict[str, Any],
) -> List[str]:
    violations: List[str] = []
    for joint_name, target_deg in joint_targets.items():
        entry = joint_limits_deg.get(joint_name)
        if not isinstance(entry, dict):
            continue
        if "min_deg" not in entry or "max_deg" not in entry:
            continue
        min_deg = float(entry["min_deg"])
        max_deg = float(entry["max_deg"])
        target = float(target_deg)
        if target < min_deg or target > max_deg:
            violations.append(
                f"{joint_name}: recorded={target:.2f} deg, allowed=[{min_deg:.2f}, {max_deg:.2f}] deg"
            )
    return violations


def _normalize_targets_for_replay(
    *,
    pose_index: int,
    joint_targets: Dict[str, float],
    joint_limits_deg: Dict[str, Any],
) -> tuple[Dict[str, float], List[str], List[str]]:
    normalized: Dict[str, float] = {}
    warnings: List[str] = []
    violations: List[str] = []

    for joint_name, raw_target_deg in joint_targets.items():
        target_deg = float(raw_target_deg)
        entry = joint_limits_deg.get(joint_name)
        if not isinstance(entry, dict) or "min_deg" not in entry or "max_deg" not in entry:
            normalized[joint_name] = target_deg
            continue

        min_deg = float(entry["min_deg"])
        max_deg = float(entry["max_deg"])
        candidates = [("recorded", target_deg)]
        if joint_name in MULTI_TURN_JOINTS and joint_name in AUTO_FLIP_REPLAY_JOINTS:
            candidates.append(("sign-flipped", -target_deg))

        best_source = "recorded"
        best_candidate = target_deg
        best_clamped = min(max(target_deg, min_deg), max_deg)
        best_violation = abs(best_candidate - best_clamped)

        for source, candidate in candidates[1:]:
            clamped = min(max(candidate, min_deg), max_deg)
            violation = abs(candidate - clamped)
            if violation + 1e-9 < best_violation:
                best_source = source
                best_candidate = candidate
                best_clamped = clamped
                best_violation = violation

        if best_source == "sign-flipped":
            warnings.append(
                f"pose={pose_index} joint={joint_name} replay target auto-flipped "
                f"{target_deg:+.2f} -> {best_candidate:+.2f} deg "
                f"for allowed range [{min_deg:+.2f}, {max_deg:+.2f}]"
            )

        if abs(best_candidate - best_clamped) > 1e-9:
            warnings.append(
                f"pose={pose_index} joint={joint_name} replay target clamped "
                f"{best_candidate:+.2f} -> {best_clamped:+.2f} deg "
                f"within [{min_deg:+.2f}, {max_deg:+.2f}]"
            )

        normalized[joint_name] = best_clamped

    return normalized, warnings, violations


def _clamp_targets_from_validation_error(
    *,
    joint_targets: Dict[str, float],
    exc: ValidationError,
) -> tuple[Dict[str, float] | None, List[str]]:
    message = str(exc)
    if "Requested joint target exceeds software safety limits from calibration:" not in message:
        return None, []

    adjusted = {joint_name: float(value) for joint_name, value in joint_targets.items()}
    warnings: List[str] = []
    changed = False

    for raw_line in message.splitlines():
        line = raw_line.strip()
        match = LIMIT_ERROR_LINE_RE.match(line)
        if match is None:
            continue
        joint_name = str(match.group("joint"))
        if joint_name not in adjusted:
            continue
        requested_deg = float(adjusted[joint_name])
        min_deg = float(match.group("min"))
        max_deg = float(match.group("max"))
        clamped_deg = min(max(requested_deg, min_deg), max_deg)
        if abs(clamped_deg - requested_deg) <= 1e-9:
            continue
        adjusted[joint_name] = clamped_deg
        changed = True
        warnings.append(
            f"joint={joint_name} runtime clamp {requested_deg:+.2f} -> {clamped_deg:+.2f} deg "
            f"within [{min_deg:+.2f}, {max_deg:+.2f}]"
        )

    return (adjusted if changed else None), warnings


def _build_replay_multi_turn_continuous_raw(
    *,
    pose: Dict[str, Any],
    replay_targets_deg: Dict[str, float],
) -> Dict[str, float]:
    recorded_targets = pose.get("joint_targets_deg")
    recorded_multi_turn_state = pose.get("multi_turn_state")
    if not isinstance(recorded_targets, dict) or not isinstance(recorded_multi_turn_state, dict):
        return {}

    overrides: Dict[str, float] = {}
    for joint_name in MULTI_TURN_JOINTS:
        if joint_name not in replay_targets_deg or joint_name not in recorded_targets:
            continue
        if abs(float(replay_targets_deg[joint_name]) - float(recorded_targets[joint_name])) > 1e-6:
            continue
        state_entry = recorded_multi_turn_state.get(joint_name)
        if not isinstance(state_entry, dict):
            continue
        continuous_raw = state_entry.get("continuous_raw")
        if not isinstance(continuous_raw, (int, float)):
            continue
        overrides[joint_name] = float(continuous_raw)
    return overrides


def _move_joints_best_effort(
    *,
    arm: SoArmMoceController,
    targets_deg: Dict[str, float],
    multi_turn_targets_continuous_raw: Dict[str, float] | None,
    duration: float,
    label: str,
) -> tuple[Dict[str, Any], Dict[str, float], Dict[str, float]]:
    requested = {joint_name: float(value) for joint_name, value in targets_deg.items()}
    requested_multi_turn = {
        joint_name: float(value)
        for joint_name, value in dict(multi_turn_targets_continuous_raw or {}).items()
        if joint_name in MULTI_TURN_JOINTS
    }
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            result = arm.move_joints(
                targets_deg=requested,
                multi_turn_targets_continuous_raw=requested_multi_turn,
                duration=float(duration),
                wait=True,
            )
            return result, requested, requested_multi_turn
        except ValidationError as exc:
            adjusted, warnings = _clamp_targets_from_validation_error(
                joint_targets=requested,
                exc=exc,
            )
            if adjusted is None:
                raise
            for warning in warnings:
                print(f"[record-pose][warn] {label} {warning}", file=sys.stderr, flush=True)
            changed_joints = {
                joint_name
                for joint_name, current_value in requested.items()
                if abs(float(adjusted[joint_name]) - float(current_value)) > 1e-9
            }
            for joint_name in changed_joints:
                requested_multi_turn.pop(joint_name, None)
            requested = adjusted
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label} move failed without a captured exception")


def _joint_errors_for_targets(
    *,
    targets_deg: Dict[str, float],
    state: Dict[str, Any],
) -> Dict[str, float]:
    joint_state = state.get("joint_state")
    if not isinstance(joint_state, dict):
        return {}
    errors: Dict[str, float] = {}
    for joint_name, target_deg in targets_deg.items():
        if joint_name not in joint_state:
            continue
        errors[joint_name] = float(joint_state[joint_name]) - float(target_deg)
    return errors


def _print_move_result_summary(
    *,
    arm: SoArmMoceController,
    label: str,
    targets_deg: Dict[str, float],
    state: Dict[str, Any],
) -> None:
    joint_state = state.get("joint_state")
    if not isinstance(joint_state, dict):
        return
    actual = {
        joint_name: float(joint_state[joint_name])
        for joint_name in targets_deg
        if joint_name in joint_state
    }
    errors = _joint_errors_for_targets(targets_deg=targets_deg, state=state)
    max_error = max((abs(value) for value in errors.values()), default=0.0)
    print(
        f"[record-pose] {label} actual="
        + json.dumps(actual, ensure_ascii=False)
        + " error="
        + json.dumps(errors, ensure_ascii=False)
        + f" max_abs_error_deg={max_error:.3f}",
        file=sys.stderr,
        flush=True,
    )

    multi_turn_error = {
        joint_name: abs(float(error_deg))
        for joint_name, error_deg in errors.items()
        if joint_name in MULTI_TURN_JOINTS
    }
    if not multi_turn_error or max(multi_turn_error.values(), default=0.0) <= 1.0:
        return

    try:
        bus = arm._ensure_bus()
        raw_present = arm._read_raw_present_position(bus)
        multi_turn_state = arm._snapshot_multi_turn_state()
        print(
            f"[record-pose][warn] {label} multi-turn follow-up raw_present="
            + json.dumps(to_jsonable(raw_present), ensure_ascii=False)
            + " multi_turn_state="
            + json.dumps(to_jsonable(multi_turn_state), ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:
            print(
                f"[record-pose][warn] {label} failed to capture multi-turn debug info: {exc}",
                file=sys.stderr,
                flush=True,
            )


def _print_gripper_result_summary(
    *,
    arm: SoArmMoceController,
    label: str,
    target_raw: int | None,
) -> None:
    if target_raw is None:
        return
    try:
        actual_raw = arm.read_gripper_raw()
    except Exception as exc:
        print(
            f"[record-pose][warn] {label} failed to read gripper raw: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    if actual_raw is None:
        return
    error_raw = int(actual_raw) - int(target_raw)
    print(
        f"[record-pose] {label} gripper target_raw={int(target_raw)} actual_raw={int(actual_raw)} error_raw={int(error_raw)}",
        file=sys.stderr,
        flush=True,
    )


def _register_56_signed_snapshot(
    *,
    arm: SoArmMoceController,
    bus,
) -> Dict[str, int]:
    raw_present = arm._read_raw_present_position(bus)
    return {
        joint_name: int(raw_present[joint_name])
        for joint_name in MULTI_TURN_JOINTS
        if joint_name in raw_present
    }


def _extract_pose_gripper_raw(pose: Dict[str, Any]) -> int | None:
    gripper = pose.get("gripper")
    if not isinstance(gripper, dict):
        return None
    value = gripper.get("present_raw")
    if not isinstance(value, (int, float)):
        return None
    return int(value)


def _restore_torque_with_current_hold(*, arm: SoArmMoceController, bus) -> Dict[str, Any]:
    # Record the exact raw position while torque is still disabled, program that
    # hold target first, then re-enable torque. This avoids the gripper snapping
    # to a stale previous goal before the replay phase starts.
    hold_state = arm.capture_hold_state(bus)
    arm.apply_hold_state(hold_state, bus=bus)
    arm.enable_torque()
    arm.set_manual_multi_turn_readback(False)
    arm.apply_hold_state(hold_state, bus=bus)
    return hold_state


def _lock_current_pose_before_exit(*, arm: SoArmMoceController) -> bool:
    try:
        bus = arm._ensure_bus()
        _restore_torque_with_current_hold(arm=arm, bus=bus)
        return True
    except Exception as exc:
        print(
            f"[record-pose][warn] 结束时未能重新锁住当前位置: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def record_pose_sequence(
    *,
    pose_count: int,
    return_duration_sec: float,
    move_duration_sec: float,
    wait_for_record_enter: bool,
    wait_between_poses: bool,
    wait_before_return_to_pose_1: bool,
    save_path: Path,
    skip_home: bool,
) -> Dict[str, Any]:
    if int(pose_count) <= 0:
        raise ValidationError("--pose-count must be >= 1")

    arm = SoArmMoceController()
    torque_was_disabled = False
    keep_torque_locked_on_close = False
    try:
        bus = arm._ensure_bus()
        gripper_available = arm.has_gripper()
        if gripper_available:
            print("[record-pose] 检测到 gripper，已纳入同一总线，本次会一起录制与回放。", file=sys.stderr, flush=True)
        else:
            print("[record-pose] 未检测到 gripper，本次自动跳过夹爪。", file=sys.stderr, flush=True)
        arm.disable_torque()
        arm.set_manual_multi_turn_readback(True)
        torque_was_disabled = True

        recorded_poses: List[Dict[str, Any]] = []
        for pose_index in range(1, int(pose_count) + 1):
            if bool(wait_for_record_enter):
                _wait_for_enter(
                    f"[record-pose] 力矩已解锁，请把机械臂摆到姿态 {pose_index}，按 Enter 录制...",
                    arm=arm,
                )
            state = arm.get_state()
            joint_targets = _record_joint_targets(state)
            raw_present = arm._read_raw_present_position(bus)
            register_56_signed = _register_56_signed_snapshot(arm=arm, bus=bus)
            multi_turn_state = arm._snapshot_multi_turn_state()
            gripper_raw = arm.read_gripper_raw()
            pose_payload = {
                "index": int(pose_index),
                "joint_targets_deg": joint_targets,
                "tcp_pose": to_jsonable(state.get("tcp_pose")),
                "raw_present_position": to_jsonable(raw_present),
                "register_56_signed": to_jsonable(register_56_signed),
                "multi_turn_state": to_jsonable(multi_turn_state),
                "gripper": {
                    "available": gripper_raw is not None,
                    "present_raw": None if gripper_raw is None else int(gripper_raw),
                },
            }
            recorded_poses.append(pose_payload)
            print(
                f"[record-pose] 已录制姿态 {pose_index}："
                + json.dumps(
                    {
                        **joint_targets,
                        **({"gripper_raw": int(gripper_raw)} if gripper_raw is not None else {}),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            if gripper_raw is not None:
                print(
                    f"[record-pose] 姿态 {pose_index} gripper_present_raw={int(gripper_raw)}",
                    file=sys.stderr,
                    flush=True,
                )
            print(
                f"[record-pose] 姿态 {pose_index} register_56_signed="
                + json.dumps(register_56_signed, ensure_ascii=False)
                + " raw_multi_turn="
                + json.dumps({name: raw_present.get(name) for name in MULTI_TURN_JOINTS}, ensure_ascii=False)
                + " state_multi_turn="
                + json.dumps(
                    {name: multi_turn_state.get(name) for name in MULTI_TURN_JOINTS},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )

        saved_path = _save_recorded_poses(save_path, recorded_poses=recorded_poses)

        recorded_pose_register_56_signed = {
            f"pose_{int(pose['index'])}": {
                joint_name: int(value)
                for joint_name, value in dict(pose.get("register_56_signed", {})).items()
                if isinstance(value, (int, float))
            }
            for pose in recorded_poses
        }
        recorded_pose_gripper_raw = {
            f"pose_{int(pose['index'])}": _extract_pose_gripper_raw(pose)
            for pose in recorded_poses
            if _extract_pose_gripper_raw(pose) is not None
        }

        meta = arm.meta()
        joint_limits_deg = meta.get("joint_limits_deg")
        if not isinstance(joint_limits_deg, dict):
            joint_limits_deg = {}

        replay_warnings: List[str] = []
        for pose in recorded_poses:
            replay_targets, pose_warnings, pose_violations = _normalize_targets_for_replay(
                pose_index=int(pose["index"]),
                joint_targets=pose["joint_targets_deg"],
                joint_limits_deg=joint_limits_deg,
            )
            pose["replay_joint_targets_deg"] = replay_targets
            pose["replay_multi_turn_continuous_raw"] = _build_replay_multi_turn_continuous_raw(
                pose=pose,
                replay_targets_deg=replay_targets,
            )
            replay_warnings.extend(pose_warnings)
            if pose_violations:
                pose["replay_limit_violations"] = list(pose_violations)

        for warning in replay_warnings:
            print(f"[record-pose][warn] {warning}", file=sys.stderr, flush=True)

        # Save the enriched replay metadata too so the last recorded session can be
        # inspected directly when we need to debug sign/limit mismatches.
        saved_path = _save_recorded_poses(save_path, recorded_poses=recorded_poses)

        first_pose = recorded_poses[0]
        first_pose_targets = first_pose.get("replay_joint_targets_deg", first_pose["joint_targets_deg"])
        first_pose_multi_turn_continuous_raw = first_pose.get("replay_multi_turn_continuous_raw", {})
        first_pose_violations = list(first_pose.get("replay_limit_violations", []))
        if first_pose_violations:
            raise ValidationError(
                "姿态 1 超出软件安全限位，无法自动返回到姿态 1：\n"
                + "\n".join(first_pose_violations)
                + "\n请先重新录制一个在安全范围内的姿态 1。"
            )

        pose_1_state: Dict[str, Any] | None = None
        if not bool(skip_home):
            if bool(wait_before_return_to_pose_1):
                _wait_for_enter(
                    f"[record-pose] 已录制完 {len(recorded_poses)} 个姿态，按 Enter 返回姿态 1...",
                    arm=arm,
                )
            _restore_torque_with_current_hold(arm=arm, bus=bus)
            torque_was_disabled = False
            print(
                f"[record-pose] 正在返回姿态 1，duration={float(return_duration_sec):.2f}s",
                file=sys.stderr,
                flush=True,
            )
            arm.write_gripper_raw(_extract_pose_gripper_raw(first_pose), bus=bus)
            # 用正常的 wait=True 轨迹让多圈关节有机会做收敛修正，避免“回到姿态 1 太快、
            # 后续姿态 2 不对位”的情况。
            pose_1_result, pose_1_targets, pose_1_multi_turn_targets = _move_joints_best_effort(
                arm=arm,
                targets_deg=first_pose_targets,
                multi_turn_targets_continuous_raw=first_pose_multi_turn_continuous_raw,
                duration=float(return_duration_sec),
                label="pose=1",
            )
            first_pose["runtime_replay_joint_targets_deg"] = pose_1_targets
            first_pose["runtime_replay_multi_turn_continuous_raw"] = pose_1_multi_turn_targets
            pose_1_state = pose_1_result["state"]
            _print_move_result_summary(
                arm=arm,
                label="pose=1 reached",
                targets_deg=pose_1_targets,
                state=pose_1_state,
            )
            _print_gripper_result_summary(
                arm=arm,
                label="pose=1 reached",
                target_raw=_extract_pose_gripper_raw(first_pose),
            )
        else:
            _restore_torque_with_current_hold(arm=arm, bus=bus)
            torque_was_disabled = False
            pose_1_state = arm.get_state()

        final_state = pose_1_state if pose_1_state is not None else arm.get_state()
        for pose in recorded_poses[1:]:
            pose_index = int(pose["index"])
            replay_targets = pose.get("replay_joint_targets_deg", pose["joint_targets_deg"])
            replay_multi_turn_continuous_raw = pose.get("replay_multi_turn_continuous_raw", {})
            violations = list(pose.get("replay_limit_violations", []))
            if violations:
                raise ValidationError(
                    f"姿态 {pose_index} 超出软件安全限位，当前已停在姿态 {pose_index - 1}：\n"
                    + "\n".join(violations)
                    + "\n请把这些关节摆回允许范围内再重新录制。"
                )
            if bool(wait_between_poses):
                _wait_for_enter(
                    f"[record-pose] 已到达姿态 {pose_index - 1}，按 Enter 前往姿态 {pose_index}..."
                )

            print(
                f"[record-pose] 正在前往姿态 {pose_index}，duration={float(move_duration_sec):.2f}s",
                file=sys.stderr,
                flush=True,
            )
            arm.write_gripper_raw(_extract_pose_gripper_raw(pose), bus=bus)
            move_result, final_replay_targets, final_replay_multi_turn_targets = _move_joints_best_effort(
                arm=arm,
                targets_deg=replay_targets,
                multi_turn_targets_continuous_raw=replay_multi_turn_continuous_raw,
                duration=float(move_duration_sec),
                label=f"pose={pose_index}",
            )
            pose["runtime_replay_joint_targets_deg"] = final_replay_targets
            pose["runtime_replay_multi_turn_continuous_raw"] = final_replay_multi_turn_targets
            final_state = move_result["state"]
            _print_move_result_summary(
                arm=arm,
                label=f"pose={pose_index} reached",
                targets_deg=final_replay_targets,
                state=final_state,
            )
            _print_gripper_result_summary(
                arm=arm,
                label=f"pose={pose_index} reached",
                target_raw=_extract_pose_gripper_raw(pose),
            )

        keep_torque_locked_on_close = _lock_current_pose_before_exit(arm=arm)

        return {
            "action": "record_pose_sequence",
            "pose_count": len(recorded_poses),
            "recorded_pose_register_56_signed": recorded_pose_register_56_signed,
            "recorded_pose_gripper_raw": recorded_pose_gripper_raw,
            "recorded_poses": recorded_poses,
            "replay_warnings": replay_warnings,
            "saved_pose_path": str(saved_path),
            "returned_to_pose_1": not bool(skip_home),
            "pose_1_state": pose_1_state,
            "final_state": final_state,
        }
    finally:
        if torque_was_disabled:
            keep_torque_locked_on_close = _lock_current_pose_before_exit(arm=arm) or keep_torque_locked_on_close
        arm.close(disable_torque=not bool(keep_torque_locked_on_close))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record multiple arm poses, return to pose 1, then replay them in order",
    )
    parser.add_argument("--pose-count", type=int, default=None)
    parser.add_argument("--return-duration-sec", "--home-duration-sec", type=float, default=5.0)
    parser.add_argument("--move-duration-sec", type=float, default=8.0)
    parser.add_argument("--wait-for-record-enter", type=cli_bool, default=True)
    parser.add_argument("--wait-between-poses", type=cli_bool, default=True)
    parser.add_argument("--wait-before-return-to-pose-1", type=cli_bool, default=True)
    parser.add_argument("--skip-home", type=cli_bool, default=False)
    parser.add_argument("--save-path", default=str(DEFAULT_SAVE_PATH))
    args = parser.parse_args()

    pose_count = int(args.pose_count) if args.pose_count is not None else _prompt_pose_count()

    try:
        print_success(
            record_pose_sequence(
                pose_count=int(pose_count),
                return_duration_sec=float(args.return_duration_sec),
                move_duration_sec=float(args.move_duration_sec),
                wait_for_record_enter=bool(args.wait_for_record_enter),
                wait_between_poses=bool(args.wait_between_poses),
                wait_before_return_to_pose_1=bool(args.wait_before_return_to_pose_1),
                save_path=Path(str(args.save_path)).expanduser(),
                skip_home=bool(args.skip_home),
            )
        )
    except Exception as exc:
        print_error(exc)


if __name__ == "__main__":
    main()
