from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
SDK_ROOT = SCRIPT_DIR.parent
REPO_ROOT = SDK_ROOT.parent
SDK_SRC = SDK_ROOT / "src"
DEFAULT_SEQUENCE_SAVE_PATH = SDK_ROOT / "workspace" / "runtime" / "recorded_motion_sequence.json"
SEQUENCE_FORMAT = "momo_hmi_joint_trajectory_v1"

if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import (  # noqa: E402
    JOINTS,
    MULTI_TURN_JOINTS,
    SoArmMoceController,
    ValidationError,
    resolve_config,
    to_jsonable,
)


def make_controller(config_path: str | Path | None = None) -> SoArmMoceController:
    return SoArmMoceController(resolve_config(config_path))


def sequence_relpath(path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except Exception:
        return str(path)


def _timestamp_slug() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def resolve_save_path(path: str | Path | None = None, *, timestamped: bool = False) -> Path:
    candidate = Path(path or DEFAULT_SEQUENCE_SAVE_PATH).expanduser()
    timestamp = _timestamp_slug()

    if candidate.exists() and candidate.is_dir():
        return (candidate / f"recorded_motion_sequence_{timestamp}.json").resolve()

    if candidate.suffix.lower() != ".json":
        return (candidate / f"recorded_motion_sequence_{timestamp}.json").resolve()

    if timestamped:
        return candidate.with_name(f"{candidate.stem}_{timestamp}{candidate.suffix}").resolve()

    return candidate.resolve()


def _newest_json_in_directory(directory: Path) -> Path | None:
    if not directory.exists() or not directory.is_dir():
        return None
    matches = sorted(
        directory.glob("recorded_motion_sequence*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        matches = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0].resolve() if matches else None


def resolve_replay_path(path: str | Path | None = None) -> Path:
    candidate = Path(path or DEFAULT_SEQUENCE_SAVE_PATH).expanduser()

    if candidate.exists() and candidate.is_dir():
        default_file = candidate / DEFAULT_SEQUENCE_SAVE_PATH.name
        if default_file.exists():
            return default_file.resolve()
        newest = _newest_json_in_directory(candidate)
        if newest is not None:
            return newest
        raise FileNotFoundError(f"目录里没有可回放的 JSON 文件: {candidate.resolve()}")

    if not candidate.exists() and candidate.suffix.lower() != ".json":
        default_file = candidate / DEFAULT_SEQUENCE_SAVE_PATH.name
        if default_file.exists():
            return default_file.resolve()
        newest = _newest_json_in_directory(candidate)
        if newest is not None:
            return newest

    resolved = candidate.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"录制文件不存在: {resolved}")
    if not resolved.is_file():
        raise ValidationError(f"录制路径不是文件: {resolved}")
    return resolved


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _joint_targets_from_joint_state(joint_state: Mapping[str, Any]) -> dict[str, float]:
    targets: dict[str, float] = {}
    for joint_name in JOINTS:
        value = joint_state.get(joint_name)
        if isinstance(value, (int, float)):
            targets[joint_name] = float(value)

    if targets:
        return targets

    names = list(joint_state.get("names") or [])
    values_rad = list(joint_state.get("values_rad") or joint_state.get("q") or [])
    if not names or not values_rad:
        return {}

    for idx in range(min(len(names), len(values_rad))):
        joint_name = str(names[idx])
        if joint_name in JOINTS:
            targets[joint_name] = math.degrees(float(values_rad[idx]))
    return targets


def build_sample_from_state(
    state: Any,
    *,
    index: int,
    started_monotonic: float,
    source: str = "robot",
) -> dict[str, Any]:
    payload = to_jsonable(state)
    if not isinstance(payload, dict):
        raise ValidationError("Robot state must be a mapping")

    now_mono = time.monotonic()
    joint_state = _mapping(payload.get("joint_state"))
    joint_targets_deg = _joint_targets_from_joint_state(joint_state)
    if not joint_targets_deg:
        raise ValidationError("Robot state is missing replayable joint targets")

    names = list(joint_state.get("names") or [joint for joint in JOINTS if joint in joint_targets_deg])
    values_rad = list(joint_state.get("values_rad") or joint_state.get("q") or [])
    if not values_rad:
        values_rad = [math.radians(float(joint_targets_deg[name])) for name in names if name in joint_targets_deg]
        names = [name for name in names if name in joint_targets_deg]

    sample: dict[str, Any] = {
        "t": float(max(0.0, now_mono - float(started_monotonic))),
        "timestamp": float(payload.get("timestamp") or time.time()),
        "source": str(source or "robot"),
        "joint_state": {
            "names": [str(name) for name in names],
            "values_rad": [float(value) for value in values_rad[: len(names)]],
        },
        "joint_targets_deg": {joint: float(joint_targets_deg[joint]) for joint in JOINTS if joint in joint_targets_deg},
        "index": int(index),
    }

    tcp_pose = payload.get("tcp_pose")
    if isinstance(tcp_pose, Mapping):
        sample["tcp_pose"] = to_jsonable(tcp_pose)

    raw_present = _mapping(payload.get("raw_present_position"))
    if raw_present:
        sample["raw_present_position"] = {
            str(name): int(value) for name, value in raw_present.items() if isinstance(value, (int, float))
        }

    relative_raw = _mapping(payload.get("relative_raw_position"))
    if relative_raw:
        sample["relative_raw_position"] = {
            str(name): float(value) for name, value in relative_raw.items() if isinstance(value, (int, float))
        }
        sample["multi_turn_targets_continuous_raw"] = {
            joint_name: float(relative_raw[joint_name])
            for joint_name in MULTI_TURN_JOINTS
            if isinstance(relative_raw.get(joint_name), (int, float))
        }

    multi_turn_state = payload.get("multi_turn_state")
    if isinstance(multi_turn_state, Mapping):
        sample["multi_turn_state"] = to_jsonable(multi_turn_state)

    gripper_state = payload.get("gripper_state")
    if not isinstance(gripper_state, Mapping):
        gripper_state = payload.get("gripper")
    gripper_payload: dict[str, Any] = {}
    if isinstance(gripper_state, Mapping):
        gripper_payload = {
            str(key): value
            for key, value in to_jsonable(gripper_state).items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        sample["gripper_state"] = gripper_payload
        for gripper_raw_key in ("present_raw", "present_register_raw", "goal_raw"):
            gripper_raw = gripper_payload.get(gripper_raw_key)
            if isinstance(gripper_raw, (int, float)):
                sample.setdefault("raw_present_position", {})["gripper"] = int(gripper_raw)
                break

    actuator_names = [str(name) for name in sample.get("raw_present_position", {}).keys()]
    if not actuator_names:
        actuator_names = [str(name) for name in names]
    elif isinstance(gripper_payload, dict) and (
        bool(gripper_payload.get("available", False)) or "gripper" in sample.get("raw_present_position", {})
    ):
        if "gripper" not in actuator_names:
            actuator_names.append("gripper")
    sample["actuator_state"] = {
        "names": actuator_names,
        "count": len(actuator_names),
    }

    return sample


def save_sequence_file(path: str | Path, *, samples: list[dict[str, Any]], sample_rate_hz: float) -> Path:
    if not samples:
        raise ValidationError("没有采样数据，未保存")

    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    duration = float(samples[-1].get("t", 0.0) or 0.0)
    actuator_order: list[str] = []
    for sample in reversed(samples):
        actuator_state = sample.get("actuator_state")
        if isinstance(actuator_state, Mapping):
            actuator_order = [str(name) for name in list(actuator_state.get("names") or [])]
            if actuator_order:
                break
        raw_present = sample.get("raw_present_position")
        if isinstance(raw_present, Mapping) and raw_present:
            actuator_order = [str(name) for name in raw_present.keys()]
            break
    payload = {
        "format": SEQUENCE_FORMAT,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_count": len(samples),
        "duration_sec": duration,
        "sample_rate_hz": float(sample_rate_hz),
        "joint_order": list(JOINTS),
        "actuator_count": len(actuator_order),
        "actuator_order": actuator_order,
        "samples": to_jsonable(samples),
    }
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return resolved


def _coerce_sequence_samples(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    samples = payload.get("samples")
    if isinstance(samples, list) and samples:
        normalized_samples: list[dict[str, Any]] = []
        for index, sample in enumerate(samples, start=1):
            if not isinstance(sample, Mapping):
                continue
            sample_payload = dict(sample)
            sample_payload.setdefault("index", int(index))
            normalized_samples.append(sample_payload)
        return normalized_samples

    poses = payload.get("poses")
    if not isinstance(poses, list) or not poses:
        return []

    converted: list[dict[str, Any]] = []
    for index, pose in enumerate(poses, start=1):
        if not isinstance(pose, Mapping):
            continue
        targets = pose.get("replay_joint_targets_deg", pose.get("joint_targets_deg"))
        if not isinstance(targets, Mapping):
            continue
        converted.append(
            {
                "index": int(index),
                "t": float(index - 1),
                "joint_targets_deg": {
                    str(name): float(value)
                    for name, value in targets.items()
                    if str(name) in JOINTS and isinstance(value, (int, float))
                },
                "multi_turn_targets_continuous_raw": {
                    str(name): float(value)
                    for name, value in dict(pose.get("replay_multi_turn_continuous_raw", {})).items()
                    if str(name) in MULTI_TURN_JOINTS and isinstance(value, (int, float))
                }
                if isinstance(pose.get("replay_multi_turn_continuous_raw"), Mapping)
                else {},
                "tcp_pose": pose.get("tcp_pose") if isinstance(pose.get("tcp_pose"), Mapping) else {},
                "gripper_state": pose.get("gripper") if isinstance(pose.get("gripper"), Mapping) else {},
            }
        )
    return converted


def load_sequence_file(path: str | Path | None = None) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    resolved = resolve_replay_path(path)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValidationError(f"录制文件格式无效，顶层必须是对象: {resolved}")
    samples = _coerce_sequence_samples(payload)
    if not samples:
        raise ValidationError(f"录制文件里没有可回放的 samples/poses: {resolved}")
    return resolved, dict(payload), samples


def targets_from_sample(sample: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    targets_raw = sample.get("joint_targets_deg")
    if isinstance(targets_raw, Mapping) and targets_raw:
        targets_deg = {
            str(name): float(value)
            for name, value in targets_raw.items()
            if str(name) in JOINTS and isinstance(value, (int, float))
        }
    else:
        joint_state = sample.get("joint_state")
        targets_deg = _joint_targets_from_joint_state(joint_state) if isinstance(joint_state, Mapping) else {}

    multi_raw = sample.get("multi_turn_targets_continuous_raw")
    if not isinstance(multi_raw, Mapping):
        multi_raw = sample.get("replay_multi_turn_continuous_raw")
    if not isinstance(multi_raw, Mapping):
        multi_raw = sample.get("relative_raw_position")
    if not isinstance(multi_raw, Mapping):
        multi_raw = {}

    multi_turn_targets = {
        joint_name: float(multi_raw[joint_name])
        for joint_name in MULTI_TURN_JOINTS
        if joint_name in targets_deg and isinstance(multi_raw.get(joint_name), (int, float))
    }
    return targets_deg, multi_turn_targets


def sample_time(sample: Any) -> float:
    if not isinstance(sample, Mapping):
        return 0.0
    try:
        return float(sample.get("t", 0.0) or 0.0)
    except Exception:
        return 0.0


def replay_sample_duration(
    samples: list[dict[str, Any]],
    index: int,
    *,
    speed: float,
    initial_duration_sec: float | None = None,
    min_step_duration_sec: float = 0.03,
    max_step_duration_sec: float = 2.0,
) -> float:
    speed_value = max(0.1, float(speed))
    if int(index) <= 0:
        if initial_duration_sec is not None:
            return max(0.0, float(initial_duration_sec))
        next_dt = 0.2
        if len(samples) > 1:
            next_dt = max(0.05, sample_time(samples[1]) - sample_time(samples[0]))
        return float(min(5.0, max(0.2, max(1.0, next_dt * 4.0) / speed_value)))

    dt = sample_time(samples[index]) - sample_time(samples[index - 1])
    if dt <= 1e-4:
        dt = 0.1
    return float(max(float(min_step_duration_sec), min(float(max_step_duration_sec), dt / speed_value)))


def gripper_raw_from_sample(sample: Mapping[str, Any]) -> int | None:
    raw_present = sample.get("raw_present_position")
    if isinstance(raw_present, Mapping):
        value = raw_present.get("gripper")
        if isinstance(value, (int, float)):
            return int(value)
    for key in ("gripper_state", "gripper"):
        payload = sample.get(key)
        if not isinstance(payload, Mapping):
            continue
        for value_key in ("present_raw", "present_register_raw", "goal_raw"):
            value = payload.get(value_key)
            if isinstance(value, (int, float)):
                return int(value)
    return None


def lock_current_pose(arm: SoArmMoceController) -> dict[str, Any]:
    bus = arm._ensure_bus()  # noqa: SLF001 - scripts need exact current-register hold.
    hold_state = arm.capture_hold_state(bus)
    arm.apply_hold_state(hold_state, bus=bus)
    arm.enable_torque()
    arm.set_manual_multi_turn_readback(False)
    arm.apply_hold_state(hold_state, bus=bus)
    return to_jsonable(hold_state)
