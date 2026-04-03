#!/usr/bin/env python3
"""Dump raw control-table registers for the current 6-motor soarmMoce bus."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus


SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk import resolve_config


DEFAULT_CONNECT_TIMEOUT_S = 8.0
DEFAULT_MOTOR_MODEL = "sts3215"
DEFAULT_MOTOR_IDS = (1, 2, 3, 4, 5, 6)
MULTI_TURN_SIGNED_PRESENT_POSITION_IDS = {2, 3}
PRESENT_POSITION_ADDRESS = 56
U16_MODULUS = 65536
SIGN_MAGNITUDE_SIGN_BIT_INDEX = 15
MOTOR_LAYOUT: dict[int, tuple[str, MotorNormMode]] = {
    1: ("shoulder_pan", MotorNormMode.DEGREES),
    2: ("shoulder_lift", MotorNormMode.DEGREES),
    3: ("elbow_flex", MotorNormMode.DEGREES),
    4: ("wrist_flex", MotorNormMode.DEGREES),
    5: ("wrist_roll", MotorNormMode.DEGREES),
    6: ("gripper", MotorNormMode.RANGE_0_100),
}


class _ConnectTimeout(RuntimeError):
    pass


def _decode_sign_magnitude_u16(raw_value: int) -> int:
    raw_u16 = int(raw_value) % U16_MODULUS
    direction_bit = (raw_u16 >> SIGN_MAGNITUDE_SIGN_BIT_INDEX) & 1
    magnitude_mask = (1 << SIGN_MAGNITUDE_SIGN_BIT_INDEX) - 1
    magnitude = raw_u16 & magnitude_mask
    return -magnitude if direction_bit else magnitude


def _parse_motor_ids(raw: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for item in str(raw or "").split(","):
        chunk = item.strip()
        if not chunk:
            continue
        try:
            motor_id = int(chunk)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid motor id: {chunk!r}") from exc
        if motor_id <= 0:
            raise argparse.ArgumentTypeError(f"motor id must be positive, got {motor_id}")
        if motor_id in seen:
            continue
        seen.add(motor_id)
        values.append(motor_id)
    if not values:
        raise argparse.ArgumentTypeError("at least one motor id is required")
    return values


def _disconnect_bus(bus: FeetechMotorsBus | None) -> None:
    if bus is None:
        return
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def _connect_bus(*, port: str, motor_ids: list[int], motor_model: str, timeout_s: float) -> tuple[FeetechMotorsBus, list[str]]:
    motors: dict[str, Motor] = {}
    motor_names: list[str] = []
    for motor_id in motor_ids:
        joint_name, norm_mode = MOTOR_LAYOUT.get(motor_id, (f"motor_{motor_id}", MotorNormMode.DEGREES))
        motors[joint_name] = Motor(int(motor_id), str(motor_model), norm_mode)
        motor_names.append(joint_name)

    bus = FeetechMotorsBus(port=port, motors=motors)

    timeout_s = float(timeout_s)
    previous_handler = None
    if timeout_s > 0.0:

        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _ConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to motor bus")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)

    try:
        bus.connect()
    except _ConnectTimeout as exc:
        _disconnect_bus(bus)
        raise RuntimeError(
            f"{exc}. Check SOARMMOCE_PORT, power, and whether another process is using the serial port."
        ) from exc
    except Exception:
        _disconnect_bus(bus)
        raise
    finally:
        if timeout_s > 0.0:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)

    return bus, motor_names


def _build_register_rows(bus: FeetechMotorsBus, motor_model: str) -> list[dict[str, Any]]:
    table = bus.model_ctrl_table.get(str(motor_model))
    if not isinstance(table, dict) or not table:
        available = ", ".join(sorted(bus.model_ctrl_table))
        raise ValueError(f"Unknown motor model '{motor_model}'. Available models: {available}")

    grouped: dict[tuple[int, int], list[str]] = {}
    for register_name, spec in table.items():
        if not isinstance(spec, tuple) or len(spec) != 2:
            continue
        address, length = int(spec[0]), int(spec[1])
        grouped.setdefault((address, length), []).append(str(register_name))

    rows: list[dict[str, Any]] = []
    for (address, length), register_names in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        rows.append(
            {
                "address": int(address),
                "address_hex": f"0x{int(address):02X}",
                "length": int(length),
                "register_names": sorted(register_names),
            }
        )
    return rows


def _read_motor_registers(
    *,
    bus: FeetechMotorsBus,
    motor_name: str,
    register_rows: list[dict[str, Any]],
    num_retry: int,
) -> dict[str, Any]:
    motor = bus.motors[motor_name]
    motor_id = int(motor.id)
    registers: list[dict[str, Any]] = []
    for row in register_rows:
        address = int(row["address"])
        length = int(row["length"])
        try:
            value, _, _ = bus._read(
                address,
                length,
                int(motor.id),
                num_retry=max(0, int(num_retry)),
                raise_on_error=True,
                err_msg=(
                    f"Failed to read raw register @{address} len={length} "
                    f"for motor '{motor_name}' id={motor.id}."
                ),
            )
            registers.append(
                {
                    **row,
                    "value": int(value),
                    "display_value": int(value),
                    "raw_value": int(value),
                    "decoder": None,
                    "error": None,
                }
            )
            if (
                motor_id in MULTI_TURN_SIGNED_PRESENT_POSITION_IDS
                and address == PRESENT_POSITION_ADDRESS
                and length == 2
            ):
                registers[-1]["display_value"] = _decode_sign_magnitude_u16(int(value))
                registers[-1]["decoder"] = "sign_magnitude_bit15_for_multi_turn_present_position"
        except Exception as exc:
            registers.append(
                {
                    **row,
                    "value": None,
                    "display_value": None,
                    "raw_value": None,
                    "decoder": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "motor_name": str(motor_name),
        "motor_id": int(motor.id),
        "motor_model": str(motor.model),
        "registers": registers,
    }


def _dump_registers(
    *,
    port: str | None,
    motor_ids: list[int],
    motor_model: str,
    timeout_s: float,
    num_retry: int,
) -> dict[str, Any]:
    config = resolve_config()
    target_port = str(port or config.port).strip()
    if not target_port:
        raise ValueError("Target port must not be empty")

    bus, motor_names = _connect_bus(
        port=target_port,
        motor_ids=motor_ids,
        motor_model=motor_model,
        timeout_s=timeout_s,
    )
    try:
        register_rows = _build_register_rows(bus, motor_model)
        motors = [
            _read_motor_registers(
                bus=bus,
                motor_name=motor_name,
                register_rows=register_rows,
                num_retry=num_retry,
            )
            for motor_name in motor_names
        ]
        return {
            "port": target_port,
            "motor_model": str(motor_model),
            "motor_ids": [int(motor_id) for motor_id in motor_ids],
            "register_count_per_motor": len(register_rows),
            "motors": motors,
        }
    finally:
        _disconnect_bus(bus)


def _print_plain_text(payload: dict[str, Any]) -> None:
    print(f"port={payload['port']} model={payload['motor_model']} register_count={payload['register_count_per_motor']}")
    print()
    for motor in payload["motors"]:
        print(f"=== motor {motor['motor_id']} {motor['motor_name']} ({motor['motor_model']}) ===")
        for register in motor["registers"]:
            address = int(register["address"])
            address_hex = str(register["address_hex"])
            names = ", ".join(register["register_names"])
            length = int(register["length"])
            if register["error"] is None:
                suffix = f"    # {names} len={length}"
                if register["decoder"]:
                    suffix += (
                        f" decoder={register['decoder']}"
                        f" raw_encoded_u16={int(register['raw_value'])}"
                    )
                print(f"{address:>3} ({address_hex}) = {int(register['display_value'])}{suffix}")
            else:
                print(
                    f"{address:>3} ({address_hex}) = <READ_ERROR>"
                    f"    # {names} len={length} :: {register['error']}"
                )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dump all known raw control-table registers for the current six soarmMoce motors "
            "and print them as address-value pairs."
        )
    )
    parser.add_argument("--port", default="", help="Serial port to use; defaults to SOARMMOCE_PORT / SDK config")
    parser.add_argument(
        "--motor-ids",
        type=_parse_motor_ids,
        default=list(DEFAULT_MOTOR_IDS),
        help="Comma-separated motor ids to read, default: 1,2,3,4,5,6",
    )
    parser.add_argument("--motor-model", default=DEFAULT_MOTOR_MODEL, help="Feetech motor model, default: sts3215")
    parser.add_argument("--timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT_S, help="Connect timeout in seconds")
    parser.add_argument("--num-retry", type=int, default=1, help="Retries per register read after the first attempt")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full dump as JSON instead of plain text",
    )
    args = parser.parse_args()

    payload = _dump_registers(
        port=args.port,
        motor_ids=list(args.motor_ids),
        motor_model=str(args.motor_model),
        timeout_s=float(args.timeout),
        num_retry=int(args.num_retry),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_plain_text(payload)


if __name__ == "__main__":
    main()
