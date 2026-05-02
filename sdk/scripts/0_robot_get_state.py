#!/usr/bin/env python3
from __future__ import annotations

import time
from typing import Any

from _robot_script_common import print_json
from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable


def main() -> None:
    # 常用参数：CONFIG_PATH=None 表示使用 SDK 默认配置。
    CONFIG_PATH = None

    # LOOP=False 只读取一次；改成 True 可以像状态监视器一样循环刷新。
    LOOP = False
    INTERVAL_SEC = 0.5

    # 只读状态脚本默认不释放扭矩，避免退出时额外写 Torque_Enable。
    RELEASE_TORQUE_ON_EXIT = False

    controller = SoArmMoceController(resolve_config(CONFIG_PATH))
    try:
        while True:
            state = controller.get_state()
            print_json(_summarize_state(state))
            if not LOOP:
                break
            time.sleep(float(INTERVAL_SEC))
    finally:
        controller.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


def _get_mapping(payload: Any, key: str) -> dict[str, Any]:
    value = payload.get(key, {}) if isinstance(payload, dict) else getattr(payload, key, {})
    return dict(value) if isinstance(value, dict) else {}


def _summarize_state(state: Any) -> dict[str, Any]:
    payload = to_jsonable(state)
    joint_state = _get_mapping(payload, "joint_state")
    raw_present = _get_mapping(payload, "raw_present_position")
    relative_raw = _get_mapping(payload, "relative_raw_position")
    startup_raw = _get_mapping(payload, "startup_raw_position")
    motor_deg = _get_mapping(payload, "motor_position_deg")
    output_deg = _get_mapping(payload, "output_position_deg")
    multi_turn_state = _get_mapping(payload, "multi_turn_state")

    joints: dict[str, dict[str, Any]] = {}
    for joint_name in JOINTS:
        joints[joint_name] = {
            "joint_deg": joint_state.get(joint_name),
            "motor_deg": motor_deg.get(joint_name),
            "output_deg": output_deg.get(joint_name),
            "raw_present": raw_present.get(joint_name),
            "relative_raw": relative_raw.get(joint_name),
            "startup_raw": startup_raw.get(joint_name),
            "multi_turn": multi_turn_state.get(joint_name),
        }

    return {
        "timestamp": payload.get("timestamp"),
        "note_cn": "只读机械臂状态；正常不会修改舵机寄存器。",
        "joint_order": list(JOINTS),
        "joints": joints,
        "tcp_pose": payload.get("tcp_pose"),
        "gripper": payload.get("gripper_state"),
    }


if __name__ == "__main__":
    main()
