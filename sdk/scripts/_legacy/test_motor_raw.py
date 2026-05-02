#!/usr/bin/env python3
"""Interactive signed-target tester for motor 3 (elbow_flex)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk import SoArmMoceController


JOINT_NAME = "wrist_roll"
MOTOR_ID = 5
GOAL_POSITION_ADDRESS = 42
PRESENT_POSITION_ADDRESS = 56
SIGN_BIT_INDEX = 15
MAX_SIGN_MAGNITUDE = (1 << SIGN_BIT_INDEX) - 1
DEFAULT_OBSERVE_S = 1.5
DEFAULT_POLL_INTERVAL_S = 0.1


def _encode_sign_magnitude_u16(value: int) -> int:
    value = int(value)
    magnitude = abs(value)
    if magnitude > MAX_SIGN_MAGNITUDE:
        raise ValueError(
            f"Target {value} exceeds sign-magnitude range [-{MAX_SIGN_MAGNITUDE}, {MAX_SIGN_MAGNITUDE}]"
        )
    direction_bit = 1 if value < 0 else 0
    return (direction_bit << SIGN_BIT_INDEX) | magnitude


def _decode_sign_magnitude_u16(value: int) -> int:
    value = int(value) & 0xFFFF
    direction_bit = (value >> SIGN_BIT_INDEX) & 1
    magnitude = value & MAX_SIGN_MAGNITUDE
    return -magnitude if direction_bit else magnitude


def _read_raw_register(bus, *, address: int) -> int:
    motor = bus.motors[JOINT_NAME]
    value, _, _ = bus._read(
        int(address),
        2,
        int(motor.id),
        num_retry=1,
        raise_on_error=True,
        err_msg=f"Failed to read register @{address} for joint '{JOINT_NAME}' id={motor.id}.",
    )
    return int(value)


def _snapshot(bus) -> dict[str, int]:
    goal_42_raw = _read_raw_register(bus, address=GOAL_POSITION_ADDRESS)
    present_56_raw = _read_raw_register(bus, address=PRESENT_POSITION_ADDRESS)
    return {
        "goal_42_raw": int(goal_42_raw),
        "goal_42_signed": int(_decode_sign_magnitude_u16(goal_42_raw)),
        "present_56_raw": int(present_56_raw),
        "present_56_signed": int(_decode_sign_magnitude_u16(present_56_raw)),
        "moving": int(bus.read("Moving", JOINT_NAME, normalize=False)),
        "velocity": int(bus.read("Present_Velocity", JOINT_NAME, normalize=False)),
        "current": int(bus.read("Present_Current", JOINT_NAME, normalize=False)),
    }


def _print_snapshot(*, label: str, snap: dict[str, int], target_signed: int | None = None) -> None:
    parts = [
        label,
        f"goal42_signed={snap['goal_42_signed']}",
        f"goal42_raw={snap['goal_42_raw']}",
        f"present56_signed={snap['present_56_signed']}",
        f"present56_raw={snap['present_56_raw']}",
        f"velocity={snap['velocity']}",
        f"current={snap['current']}",
        f"moving={snap['moving']}",
    ]
    if target_signed is not None:
        parts.append(f"error={int(snap['present_56_signed'] - int(target_signed))}")
    print(" | ".join(parts), flush=True)


def _observe_after_command(
    *,
    bus,
    target_signed: int,
    observe_s: float,
    poll_interval_s: float,
) -> None:
    observe_s = max(0.0, float(observe_s))
    poll_interval_s = max(0.01, float(poll_interval_s))
    deadline = time.monotonic() + observe_s
    sample_index = 0
    while True:
        snap = _snapshot(bus)
        elapsed = max(0.0, observe_s - max(0.0, deadline - time.monotonic()))
        _print_snapshot(
            label=f"[sample {sample_index:02d} t+{elapsed:.2f}s]",
            snap=snap,
            target_signed=target_signed,
        )
        sample_index += 1
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive test tool for motor 3 signed target values and register 42/56 readback",
    )
    parser.add_argument("--observe-s", type=float, default=DEFAULT_OBSERVE_S, help="Observe time after each command")
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Polling interval while observing",
    )
    args = parser.parse_args()

    arm = SoArmMoceController()
    try:
        bus = arm._ensure_bus()
        print(
            f"Interactive {JOINT_NAME} tester ready: joint={JOINT_NAME} motor_id={MOTOR_ID}.",
            flush=True,
        )
        print(
            "Input a signed target integer; the script will also show the encoded register raw value. "
            "Press Enter to read current state, or type q to quit.",
            flush=True,
        )
        _print_snapshot(label="[initial]", snap=_snapshot(bus))

        while True:
            raw = input("motor3 target signed> ").strip()
            if raw.lower() in {"q", "quit", "exit"}:
                break
            if raw.lower() in {"h", "help", "?"}:
                print(
                    "Enter a signed target integer such as -12000 or 8000. "
                    "The encoded 42/56 raw register values will be shown separately. "
                    "Empty input just reads current state."
                )
                continue
            if not raw or raw.lower() in {"r", "read"}:
                _print_snapshot(label="[read]", snap=_snapshot(bus))
                continue

            try:
                target_signed = int(raw)
                target_encoded_u16 = _encode_sign_magnitude_u16(target_signed)
            except Exception as exc:
                print(f"[error] {exc}", flush=True)
                continue

            bus.write("Goal_Position", JOINT_NAME, int(target_signed), normalize=False)
            print(
                f"[cmd] target_signed={target_signed} target_encoded_u16={target_encoded_u16}",
                flush=True,
            )
            _observe_after_command(
                bus=bus,
                target_signed=target_signed,
                observe_s=float(args.observe_s),
                poll_interval_s=float(args.poll_interval_s),
            )
    finally:
        arm.close()


if __name__ == "__main__":
    main()
