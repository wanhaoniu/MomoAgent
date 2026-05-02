#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import (
    DEFAULT_PORT,
    disconnect_bus,
    make_single_motor_bus,
    print_json,
    read_register_raw,
    select_registers,
)


def main() -> None:
    PORT = DEFAULT_PORT
    BAUDRATE = 1_000_000
    PROTOCOL_VERSION = 0
    MODEL = "sts3215"

    # 常用参数：改 MOTOR_ID 选择舵机。
    MOTOR_ID = 1

    # 默认读最常用的状态寄存器。想读特定寄存器时，把 key 写到这里。
    REGISTER_KEYS: list[str] | None = None

    # 想按组读取时使用，例如 "eprom"、"sram_control"、"sram_feedback"。
    REGISTER_GROUP: str | None = None

    # True 会读取 docs/1.txt 里整理出来的全部寄存器，输出较长。
    READ_ALL_DOCUMENTED = False

    specs = select_registers(
        keys=REGISTER_KEYS,
        group=REGISTER_GROUP,
        read_all=READ_ALL_DOCUMENTED,
    )

    bus = make_single_motor_bus(
        port=PORT,
        motor_id=MOTOR_ID,
        model=MODEL,
        baudrate=BAUDRATE,
        protocol_version=PROTOCOL_VERSION,
    )
    try:
        registers = [read_register_raw(bus, MOTOR_ID, spec) for spec in specs]
    finally:
        disconnect_bus(bus, disable_torque=False)

    print_json(
        {
            "port": PORT,
            "baudrate": BAUDRATE,
            "protocol_version": PROTOCOL_VERSION,
            "model": MODEL,
            "motor_id": MOTOR_ID,
            "selected_registers": [spec.key for spec in specs],
            "registers": registers,
        }
    )


if __name__ == "__main__":
    main()
