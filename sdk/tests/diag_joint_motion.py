from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable


DEFAULT_REGISTERS = (
    "Operating_Mode",
    "Phase",
    "Min_Position_Limit",
    "Max_Position_Limit",
    "Torque_Enable",
    "Lock",
    "Goal_Position",
    "Present_Position",
    "Moving",
    "Present_Load",
    "Present_Current",
    "Present_Voltage",
    "Present_Temperature",
    "Status",
)


def _parse_targets(text: str) -> list[float]:
    values: list[float] = []
    for chunk in str(text or "").split(","):
        raw = chunk.strip()
        if not raw:
            continue
        values.append(float(raw))
    if not values:
        raise argparse.ArgumentTypeError("targets list must not be empty")
    return values


def _read_snapshot(controller: SoArmMoceController, joint_name: str) -> dict[str, Any]:
    bus = controller._ensure_bus()
    state = controller.get_state()
    goal_raw = None
    present_raw = None
    registers: dict[str, Any] = {}
    for register_name in DEFAULT_REGISTERS:
        try:
            value = int(bus.read(register_name, joint_name, normalize=False))
        except Exception as exc:
            value = f"{type(exc).__name__}: {exc}"
        registers[register_name] = value
        if register_name == "Goal_Position" and isinstance(value, int):
            goal_raw = value
        if register_name == "Present_Position" and isinstance(value, int):
            present_raw = value

    goal_error_raw = None
    if goal_raw is not None and present_raw is not None:
        try:
            goal_error_raw = int(controller._goal_error_raw(joint_name, present_raw=present_raw, goal_raw=goal_raw))
        except Exception:
            goal_error_raw = None

    return {
        "timestamp": time.time(),
        "joint": str(joint_name),
        "joint_deg": float(state["joint_state"][joint_name]),
        "relative_raw": int(state["relative_raw_position"][joint_name]),
        "startup_raw": int(state["startup_raw_position"][joint_name]),
        "goal_error_raw": goal_error_raw,
        "registers": registers,
    }


def _print_event(event: str, payload: dict[str, Any]) -> None:
    print(
        json.dumps(
            {
                "event": str(event),
                **to_jsonable(payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _sample_window(
    controller: SoArmMoceController,
    *,
    joint_name: str,
    observe_sec: float,
    sample_interval_sec: float,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(0.0, float(observe_sec))
    while True:
        snapshot = _read_snapshot(controller, joint_name)
        snapshot["elapsed_sec"] = max(0.0, float(observe_sec) - max(0.0, deadline - time.monotonic()))
        samples.append(snapshot)
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, float(sample_interval_sec)))
    return samples


def _run_target_sequence(
    controller: SoArmMoceController,
    *,
    joint_name: str,
    targets_deg: list[float],
    move_duration_sec: float,
    observe_sec: float,
    sample_interval_sec: float,
    settle_timeout_sec: float,
    home_between: bool,
) -> None:
    _print_event("initial", _read_snapshot(controller, joint_name))

    for target_deg in targets_deg:
        _print_event(
            "command",
            {
                "joint": joint_name,
                "target_deg": float(target_deg),
                "move_duration_sec": float(move_duration_sec),
            },
        )
        command_error: str | None = None
        try:
            result = controller.move_joint(
                joint=joint_name,
                target_deg=float(target_deg),
                duration=float(move_duration_sec),
                wait=False,
                timeout=float(settle_timeout_sec),
            )
            _print_event(
                "command_result",
                {
                    "target_deg": float(target_deg),
                    "goal_raw": dict(result.get("goal_raw", {})),
                    "targets_deg": dict(result.get("targets_deg", {})),
                },
            )
        except Exception as exc:
            command_error = f"{type(exc).__name__}: {exc}"
            _print_event("command_error", {"target_deg": float(target_deg), "message": command_error})

        for sample in _sample_window(
            controller,
            joint_name=joint_name,
            observe_sec=float(observe_sec),
            sample_interval_sec=float(sample_interval_sec),
        ):
            _print_event("sample", sample)

        if home_between:
            _print_event("home_command", {"joint": joint_name})
            try:
                controller.home(duration=float(move_duration_sec), wait=False, timeout=float(settle_timeout_sec))
            except Exception as exc:
                _print_event("home_command_error", {"message": f"{type(exc).__name__}: {exc}"})
            for sample in _sample_window(
                controller,
                joint_name=joint_name,
                observe_sec=float(observe_sec),
                sample_interval_sec=float(sample_interval_sec),
            ):
                _print_event("home_sample", sample)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-joint hardware diagnostic helper for the rebuilt SoArmMoce SDK."
    )
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--joint", type=str, default="shoulder_lift", choices=JOINTS)
    parser.add_argument("--targets-deg", type=_parse_targets, default=[2.0, 5.0, -2.0, -5.0])
    parser.add_argument("--move-duration-sec", type=float, default=1.2)
    parser.add_argument("--observe-sec", type=float, default=2.5)
    parser.add_argument("--sample-interval-sec", type=float, default=0.1)
    parser.add_argument("--settle-timeout-sec", type=float, default=4.0)
    parser.add_argument("--no-home-between", action="store_true")
    parser.add_argument("--release-torque-on-exit", action="store_true")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    controller = SoArmMoceController(resolve_config(args.config))

    try:
        controller._ensure_bus()
        _run_target_sequence(
            controller,
            joint_name=str(args.joint),
            targets_deg=list(args.targets_deg),
            move_duration_sec=float(args.move_duration_sec),
            observe_sec=float(args.observe_sec),
            sample_interval_sec=float(args.sample_interval_sec),
            settle_timeout_sec=float(args.settle_timeout_sec),
            home_between=not bool(args.no_home_between),
        )
        return 0
    finally:
        controller.close(disable_torque=bool(args.release_torque_on_exit))


if __name__ == "__main__":
    raise SystemExit(main())
