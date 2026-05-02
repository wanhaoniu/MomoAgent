#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import (
    clamp_open_ratio,
    make_controller,
    print_json,
    summarize_gripper_state,
    summarize_motion_result,
)


def main() -> None:
    CONFIG_PATH = None

    # 可选动作：
    # - "open": 打开夹爪。
    # - "close": 关闭夹爪。
    # - "set": 按 OPEN_RATIO 设置开合比例，0.0=关，1.0=开。
    ACTION = "open"
    OPEN_RATIO = 0.5

    DURATION_SEC: float | None = 1.0
    SPEED_PERCENT = 40
    WAIT = True
    TIMEOUT_SEC = 3.0
    RELEASE_TORQUE_ON_EXIT = False

    action = str(ACTION).strip().lower()
    valid_actions = {"open", "close", "set"}
    if action not in valid_actions:
        raise ValueError(f"ACTION 必须是 {sorted(valid_actions)} 之一，当前是 {ACTION!r}。")
    open_ratio = clamp_open_ratio(float(OPEN_RATIO))

    arm = make_controller(CONFIG_PATH)
    try:
        result: dict[str, object] = {
            "action": action,
            "open_ratio": open_ratio,
            "note_cn": "夹爪比例 0.0=关闭，1.0=打开；如果硬件没有夹爪，SDK 会直接报错。",
            "before_gripper": summarize_gripper_state(arm.get_gripper_state()),
        }

        if action == "open":
            command_result = arm.open_gripper(
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )
        elif action == "close":
            command_result = arm.close_gripper(
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )
        else:
            command_result = arm.set_gripper(
                open_ratio=open_ratio,
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )

        result["command"] = summarize_motion_result(command_result)
        result["after_gripper"] = summarize_gripper_state(arm.get_gripper_state())
        print_json(result)
    finally:
        arm.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    main()
