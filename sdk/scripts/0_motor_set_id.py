#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import (
    DEFAULT_PORT,
    REGISTER_BY_KEY,
    disconnect_bus,
    ensure_ok,
    make_bus,
    make_single_motor_bus,
    ping_motor,
    print_json,
    read_register_raw,
    write_register_raw,
)


def main() -> None:
    # 改 ID 前请只接一个目标舵机，尤其不要让多个同 ID 舵机同时挂在总线上。
    PORT = DEFAULT_PORT
    BAUDRATE = 1_000_000
    PROTOCOL_VERSION = 0
    MODEL = "sts3215"

    # 常用参数：先把 CURRENT_ID 改成舵机当前 ID，再把 NEW_ID 改成目标 ID。
    # NEW_ID=None 时只读取当前舵机信息，不会写寄存器，适合先确认连线。
    CURRENT_ID = 1
    NEW_ID: int | None = None

    # True 时用 broadcast_ping 做一次保护检查；如果看到多个不同 ID，会拒绝改名。
    REQUIRE_ONLY_ONE_RESPONDING_ID = True

    validate_motor_id(CURRENT_ID, label="CURRENT_ID")
    if NEW_ID is not None:
        validate_motor_id(NEW_ID, label="NEW_ID")
        if int(CURRENT_ID) == int(NEW_ID):
            raise ValueError("CURRENT_ID 和 NEW_ID 相同，不需要修改。")

    result: dict[str, object] = {
        "port": PORT,
        "baudrate": BAUDRATE,
        "protocol_version": PROTOCOL_VERSION,
        "model": MODEL,
        "current_id": CURRENT_ID,
        "new_id": NEW_ID,
        "note_cn": "改 ID 是 EPROM 配置操作；请确认总线上只有一个目标舵机。",
        "before": read_motor_info(
            port=PORT,
            baudrate=BAUDRATE,
            protocol_version=PROTOCOL_VERSION,
            model=MODEL,
            motor_id=CURRENT_ID,
        ),
    }

    if NEW_ID is None:
        result["write_skipped_cn"] = "NEW_ID=None，本次只读当前 ID 信息，没有写寄存器。"
        print_json(result)
        return

    if REQUIRE_ONLY_ONE_RESPONDING_ID:
        result["single_motor_check"] = check_single_motor_on_bus(
            port=PORT,
            baudrate=BAUDRATE,
            protocol_version=PROTOCOL_VERSION,
        )

    result["rename"] = rename_motor(
        port=PORT,
        baudrate=BAUDRATE,
        protocol_version=PROTOCOL_VERSION,
        model=MODEL,
        current_id=CURRENT_ID,
        new_id=int(NEW_ID),
    )
    result["after"] = read_motor_info(
        port=PORT,
        baudrate=BAUDRATE,
        protocol_version=PROTOCOL_VERSION,
        model=MODEL,
        motor_id=int(NEW_ID),
    )
    print_json(result)


def validate_motor_id(value: int, *, label: str) -> None:
    if not 0 <= int(value) <= 253:
        raise ValueError(f"{label} 必须在 0~253 之间，当前是 {value}。")


def read_motor_info(*, port: str, baudrate: int, protocol_version: int, model: str, motor_id: int) -> dict[str, object]:
    bus = make_single_motor_bus(
        port=port,
        motor_id=motor_id,
        model=model,
        baudrate=baudrate,
        protocol_version=protocol_version,
    )
    try:
        model_number = ping_motor(bus, "target")
        if model_number is None:
            raise ConnectionError(f"舵机 ID {motor_id} 在 {port} / {baudrate} 下没有响应。")
        id_register = read_register_raw(bus, motor_id, REGISTER_BY_KEY["ID"])
        ensure_ok(id_register, action="read ID")
        return {
            "motor_id": int(motor_id),
            "model_number": int(model_number),
            "id_register": int(id_register["raw"]),
        }
    finally:
        disconnect_bus(bus, disable_torque=False)


def check_single_motor_on_bus(*, port: str, baudrate: int, protocol_version: int) -> dict[str, object]:
    bus = make_bus(port=port, baudrate=baudrate, protocol_version=protocol_version, motors={})
    try:
        broadcast_ping = getattr(bus, "broadcast_ping", None)
        if not callable(broadcast_ping):
            return {"checked": False, "reason": "broadcast_ping unavailable"}
        try:
            found = broadcast_ping(num_retry=1, raise_on_error=False)
        except TypeError:
            found = broadcast_ping(raise_on_error=False)
    finally:
        disconnect_bus(bus, disable_torque=False)

    found = found or {}
    if len(found) > 1:
        raise RuntimeError(f"总线上响应了多个不同 ID: {sorted(found)}。请只接一个目标舵机后再改 ID。")
    return {"checked": True, "broadcast_ping": found, "responding_count": len(found)}


def rename_motor(
    *,
    port: str,
    baudrate: int,
    protocol_version: int,
    model: str,
    current_id: int,
    new_id: int,
) -> dict[str, object]:
    bus = make_single_motor_bus(
        port=port,
        motor_id=current_id,
        model=model,
        baudrate=baudrate,
        protocol_version=protocol_version,
    )
    try:
        disable_torque = getattr(bus, "disable_torque", None)
        if callable(disable_torque):
            try:
                disable_torque("target", num_retry=1)
            except TypeError:
                disable_torque("target")

        write_payload = write_register_raw(bus, current_id, REGISTER_BY_KEY["ID"], int(new_id))
        ensure_ok(write_payload, action="write ID")
    finally:
        disconnect_bus(bus, disable_torque=False)

    return {"from": int(current_id), "to": int(new_id), "write": write_payload}


if __name__ == "__main__":
    main()
