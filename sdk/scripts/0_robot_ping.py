#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import DEFAULT_PORT, disconnect_bus, make_bus, make_motor, ping_motor, print_json, require_lerobot


def main() -> None:
    # 常用参数：默认扫机械臂 1~6，再多留几个 ID 方便查总线上有没有临时舵机。
    PORT = DEFAULT_PORT
    BAUDRATE = 1_000_000
    PROTOCOL_VERSION = 0
    MODEL = "sts3215"
    IDS_TO_PING = range(1, 12)

    # 改成 True 时，会调用 LeRobot 的 scan_port 跨波特率扫描，速度会慢一些。
    RUN_SCAN_PORT = False

    FeetechMotorsBus, _, _ = require_lerobot()
    ids = [int(motor_id) for motor_id in IDS_TO_PING]
    motors = {f"id_{motor_id}": make_motor(motor_id, MODEL) for motor_id in ids}

    result: dict[str, object] = {
        "port": PORT,
        "baudrate": BAUDRATE,
        "protocol_version": PROTOCOL_VERSION,
        "model": MODEL,
        "ids_to_ping": ids,
    }

    if RUN_SCAN_PORT:
        result["scan_port"] = FeetechMotorsBus.scan_port(PORT, protocol_version=PROTOCOL_VERSION)

    bus = make_bus(port=PORT, baudrate=BAUDRATE, protocol_version=PROTOCOL_VERSION, motors=motors)
    try:
        broadcast_ping = getattr(bus, "broadcast_ping", None)
        if callable(broadcast_ping):
            try:
                result["broadcast_ping"] = broadcast_ping(num_retry=1, raise_on_error=False)
            except TypeError:
                result["broadcast_ping"] = broadcast_ping(raise_on_error=False)

        ping_result: dict[str, object] = {}
        for motor_id in ids:
            model_number = ping_motor(bus, f"id_{motor_id}")
            ping_result[str(motor_id)] = None if model_number is None else int(model_number)
        result["ping"] = ping_result
    finally:
        disconnect_bus(bus, disable_torque=False)

    responding_ids = set()
    broadcast = result.get("broadcast_ping")
    if isinstance(broadcast, dict):
        responding_ids.update(int(motor_id) for motor_id in broadcast.keys())
    for motor_id, model_number in dict(result.get("ping", {})).items():
        if model_number is not None:
            responding_ids.add(int(motor_id))

    result["responding_ids"] = sorted(responding_ids)
    result["responding_count"] = len(responding_ids)
    print_json(result)


if __name__ == "__main__":
    main()
