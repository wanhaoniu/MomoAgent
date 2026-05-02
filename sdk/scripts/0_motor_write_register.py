#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import (
    DEFAULT_PORT,
    disconnect_bus,
    ensure_ok,
    make_single_motor_bus,
    print_json,
    read_register_raw,
    require_register,
    write_register_raw,
)


def main() -> None:
    PORT = DEFAULT_PORT
    BAUDRATE = 1_000_000
    PROTOCOL_VERSION = 0
    MODEL = "sts3215"

    # 常用参数：改 MOTOR_ID、REGISTER_KEY、NEW_VALUE 即可。
    # REGISTER_KEY 来自 _robot_script_common.py 里的 DOCUMENTED_REGISTERS。
    MOTOR_ID = 1
    REGISTER_KEY = "Torque_Limit"
    NEW_VALUE = 600

    # 安全开关：默认只读当前值。确认要写硬件时，手动改成 True。
    WRITE_ENABLE = False

    spec = require_register(REGISTER_KEY)
    result: dict[str, object] = {
        "port": PORT,
        "baudrate": BAUDRATE,
        "protocol_version": PROTOCOL_VERSION,
        "model": MODEL,
        "motor_id": MOTOR_ID,
        "register_key": REGISTER_KEY,
        "new_value": NEW_VALUE,
        "write_enable": WRITE_ENABLE,
    }

    bus = make_single_motor_bus(
        port=PORT,
        motor_id=MOTOR_ID,
        model=MODEL,
        baudrate=BAUDRATE,
        protocol_version=PROTOCOL_VERSION,
    )
    try:
        before = read_register_raw(bus, MOTOR_ID, spec)
        ensure_ok(before, action=f"read {REGISTER_KEY} before write")
        result["before"] = before

        if not WRITE_ENABLE:
            result["write_skipped_cn"] = "WRITE_ENABLE=False，本次只读当前值，没有写寄存器。"
            print_json(result)
            return

        if spec.read_only:
            raise ValueError(f"{spec.key} / {spec.label_cn} 是只读寄存器，不能写入。")
        if spec.group == "eprom":
            result["warning_cn"] = "正在写 EPROM 配置寄存器；请确认该数值来自 docs/1.txt，且电源和总线稳定。"

        write_payload = write_register_raw(bus, MOTOR_ID, spec, int(NEW_VALUE))
        ensure_ok(write_payload, action=f"write {REGISTER_KEY}")
        result["write"] = write_payload

        after = read_register_raw(bus, MOTOR_ID, spec)
        ensure_ok(after, action=f"read {REGISTER_KEY} after write")
        result["after"] = after
        result["verified"] = int(after["raw"]) == int(NEW_VALUE)
    finally:
        disconnect_bus(bus, disable_torque=False)

    print_json(result)


if __name__ == "__main__":
    main()
