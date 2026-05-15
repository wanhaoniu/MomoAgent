#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


RUNTIME_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = RUNTIME_ROOT.parent


def _candidate_sdk_paths() -> list[Path]:
    candidates: list[Path] = []
    env_sdk = os.environ.get("MOMOAGENT_SDK_SRC", "").strip()
    if env_sdk:
        candidates.append(Path(env_sdk).expanduser())
    env_repo = os.environ.get("MOMOAGENT_REPO_ROOT", "").strip()
    if env_repo:
        candidates.append(Path(env_repo).expanduser() / "sdk" / "src")
    candidates.extend(
        [
            PLUGIN_ROOT / "sdk" / "src",
            PLUGIN_ROOT / "runtime" / "third_party" / "MomoAgent" / "sdk" / "src",
            PLUGIN_ROOT.parent / "sdk" / "src",
            PLUGIN_ROOT.parent.parent / "sdk" / "src",
            PLUGIN_ROOT.parent.parent.parent / "sdk" / "src",
        ]
    )
    return candidates


def _ensure_sdk_path() -> Path | None:
    for candidate in _candidate_sdk_paths():
        src = candidate.expanduser().resolve()
        if (src / "soarmmoce_sdk" / "__init__.py").is_file():
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            return src
    return None


SDK_SRC = _ensure_sdk_path()


def _load_sdk():
    try:
        from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Unable to import soarmmoce_sdk. Set MOMOAGENT_REPO_ROOT or MOMOAGENT_SDK_SRC, "
            "or copy MomoAgent/sdk into the plugin repository."
        ) from exc
    return JOINTS, SoArmMoceController, resolve_config, to_jsonable


def _json_default(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.integer, np.floating)):
            return value.item()
    except Exception:
        pass
    return str(value)


def print_json(payload: dict[str, Any], *, pretty: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, sort_keys=True, default=_json_default))


def _as_params(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --params-json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--params-json must be a JSON object")
    return parsed


def _read_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _read_float(params: dict[str, Any], name: str, default: float | None = None) -> float | None:
    value = params.get(name, default)
    if value is None or value == "":
        return None
    return float(value)


def _read_int(params: dict[str, Any], name: str, default: int | None = None) -> int | None:
    value = params.get(name, default)
    if value is None or value == "":
        return None
    return int(value)


def _read_xyz(params: dict[str, Any]) -> list[float]:
    value = params.get("xyz")
    if value is None:
        value = [params.get("x"), params.get("y"), params.get("z")]
    if isinstance(value, str):
        value = json.loads(value)
    values = list(value)
    if len(values) != 3:
        raise ValueError("xyz must contain exactly 3 values")
    return [float(item) for item in values]


def _read_rpy(params: dict[str, Any]) -> list[float] | None:
    value = params.get("rpy")
    if value is None or value == "":
        if all(key in params for key in ("roll", "pitch", "yaw")):
            value = [params["roll"], params["pitch"], params["yaw"]]
        else:
            return None
    if isinstance(value, str):
        value = json.loads(value)
    values = list(value)
    if len(values) != 3:
        raise ValueError("rpy must contain exactly 3 values")
    return [float(item) for item in values]


def _state_summary(state: Any, to_jsonable) -> dict[str, Any]:
    payload = to_jsonable(state)
    joint_state = dict(payload.get("joint_state") or {})
    joints_deg = {
        str(joint_name): joint_state.get(joint_name)
        for joint_name in list(joint_state.get("names") or [])
        if joint_name in joint_state
    }
    return {
        "timestamp": payload.get("timestamp"),
        "joint_order": list(joint_state.get("names") or []),
        "joints_deg": joints_deg,
        "joints_rad": list(joint_state.get("q") or []),
        "tcp_pose": payload.get("tcp_pose"),
        "gripper": payload.get("gripper_state"),
        "raw_present_position": payload.get("raw_present_position"),
        "relative_raw_position": payload.get("relative_raw_position"),
    }


def _result_summary(result: Any, to_jsonable) -> dict[str, Any]:
    payload = to_jsonable(result)
    if not isinstance(payload, dict):
        return {"value": payload}
    out: dict[str, Any] = {}
    for key in (
        "action",
        "target_deg",
        "targets_deg",
        "goal_raw",
        "duration_sec",
        "duration_source",
        "speed_percent",
        "frame",
        "delta",
        "target_xyz_m",
        "target_rpy_rad",
        "composed_target_rpy_rad",
        "orientation_mode",
        "ik",
        "wait",
        "settled",
    ):
        if key in payload:
            out[key] = payload[key]
    if "state" in payload:
        out["state"] = _state_summary(payload["state"], to_jsonable)
    return out


def _make_arm(params: dict[str, Any]):
    _, SoArmMoceController, resolve_config, _ = _load_sdk()
    config_path = str(params.get("config") or os.environ.get("SOARMMOCE_CONFIG", "") or "").strip() or None
    return SoArmMoceController(resolve_config(config_path))


def _motion_kwargs(params: dict[str, Any], *, default_duration: float, default_speed: int, default_timeout: float) -> dict[str, Any]:
    return {
        "duration": _read_float(params, "duration", default_duration),
        "speed_percent": _read_int(params, "speed_percent", default_speed),
        "wait": _read_bool(params.get("wait"), True),
        "timeout": _read_float(params, "timeout", default_timeout),
    }


def _run_with_arm(action: str, params: dict[str, Any]) -> dict[str, Any]:
    JOINTS, _, _, to_jsonable = _load_sdk()
    arm = _make_arm(params)
    release_torque = _read_bool(params.get("release_torque_on_exit"), False)
    try:
        if action == "state":
            return {"ok": True, "action": action, "state": _state_summary(arm.get_state(), to_jsonable)}

        before = _state_summary(arm.get_state(), to_jsonable)

        if action == "joint_delta":
            joint = str(params.get("joint") or params.get("joint_name") or "").strip()
            if joint not in JOINTS:
                raise ValueError(f"unknown joint: {joint}")
            result = arm.move_joint(
                joint=joint,
                delta_deg=float(params["delta_deg"]),
                **_motion_kwargs(params, default_duration=1.0, default_speed=30, default_timeout=4.0),
            )
        elif action == "joint_target":
            joint = str(params.get("joint") or params.get("joint_name") or "").strip()
            if joint not in JOINTS:
                raise ValueError(f"unknown joint: {joint}")
            result = arm.move_joint(
                joint=joint,
                target_deg=float(params["target_deg"]),
                **_motion_kwargs(params, default_duration=1.0, default_speed=30, default_timeout=4.0),
            )
        elif action == "joints_target":
            targets = params.get("targets_deg") or params.get("targets") or {}
            if isinstance(targets, str):
                targets = json.loads(targets)
            result = arm.move_joints(
                {str(name): float(value) for name, value in dict(targets).items()},
                **_motion_kwargs(params, default_duration=1.5, default_speed=30, default_timeout=5.0),
            )
        elif action == "cartesian_delta":
            result = arm.move_delta(
                dx=float(params.get("dx", 0.0) or 0.0),
                dy=float(params.get("dy", 0.0) or 0.0),
                dz=float(params.get("dz", 0.0) or 0.0),
                drx=float(params.get("drx", 0.0) or 0.0),
                dry=float(params.get("dry", 0.0) or 0.0),
                drz=float(params.get("drz", 0.0) or 0.0),
                frame=str(params.get("frame", "base") or "base").strip().lower(),
                **_motion_kwargs(params, default_duration=1.0, default_speed=25, default_timeout=5.0),
            )
        elif action == "pose":
            result = arm.move_pose(
                xyz=_read_xyz(params),
                rpy=_read_rpy(params),
                seed_policy=str(params.get("seed_policy", "current") or "current"),
                **_motion_kwargs(params, default_duration=2.0, default_speed=25, default_timeout=8.0),
            )
        elif action == "gripper":
            ratio = float(params.get("open_ratio"))
            result = arm.set_gripper(
                open_ratio=max(0.0, min(1.0, ratio)),
                **_motion_kwargs(params, default_duration=1.0, default_speed=40, default_timeout=3.0),
            )
        elif action == "open_gripper":
            result = arm.open_gripper(**_motion_kwargs(params, default_duration=1.0, default_speed=40, default_timeout=3.0))
        elif action == "close_gripper":
            result = arm.close_gripper(**_motion_kwargs(params, default_duration=1.0, default_speed=40, default_timeout=3.0))
        elif action == "home":
            result = arm.home(**_motion_kwargs(params, default_duration=1.5, default_speed=30, default_timeout=5.0))
        elif action == "stop":
            result = arm.stop()
        elif action in {"torque", "enable_torque", "disable_torque"}:
            mode = str(params.get("mode") or action).strip().lower()
            if mode in {"enable", "enable_torque"}:
                arm.enable_torque()
                result = {"action": "enable_torque"}
            elif mode in {"disable", "disable_torque"}:
                arm.disable_torque()
                result = {"action": "disable_torque"}
            else:
                raise ValueError("torque mode must be enable or disable")
        else:
            raise ValueError(f"unsupported action: {action}")

        return {
            "ok": True,
            "action": action,
            "before": before,
            "result": _result_summary(result, to_jsonable),
            "after": _state_summary(arm.get_state(), to_jsonable),
        }
    finally:
        arm.close(disable_torque=release_torque)


def run_action(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        if action == "preflight":
            sdk_path = _ensure_sdk_path()
            _load_sdk()
            payload: dict[str, Any] = {
                "ok": True,
                "action": "preflight",
                "sdk_src": str(sdk_path or SDK_SRC or ""),
                "connected": False,
            }
            if _read_bool(params.get("connect"), False):
                state_payload = _run_with_arm("state", params)
                payload["connected"] = bool(state_payload.get("ok"))
                payload["state"] = state_payload.get("state")
            return payload
        return _run_with_arm(action, params)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "action": action,
            "error": str(exc).strip() or exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MomoAgent direct SDK bridge for PhyAgentOS.")
    parser.add_argument("action")
    parser.add_argument("--params-json", default="{}")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    payload = run_action(str(args.action).strip().lower(), _as_params(args.params_json))
    print_json(payload, pretty=bool(args.pretty))
    return 0 if bool(payload.get("ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
