#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import make_controller, print_json, summarize_motion_result, summarize_state


def main() -> None:
    CONFIG_PATH = None

    # 可选动作：
    # - "home": 回到本次连接时的启动姿态零位。
    # - "stop": 把当前 raw 位置写成保持目标，用于立即保持当前位置。
    # - "enable_torque": 打开扭矩。
    # - "disable_torque": 关闭扭矩，机械臂会掉力，请扶住机械臂。
    ACTION = "home"

    DURATION_SEC: float | None = 1.5
    SPEED_PERCENT = 30
    WAIT = True
    TIMEOUT_SEC = 5.0
    RELEASE_TORQUE_ON_EXIT = False

    action = str(ACTION).strip().lower()
    valid_actions = {"home", "stop", "enable_torque", "disable_torque"}
    if action not in valid_actions:
        raise ValueError(f"ACTION 必须是 {sorted(valid_actions)} 之一，当前是 {ACTION!r}。")

    arm = make_controller(CONFIG_PATH)
    try:
        result: dict[str, object] = {
            "action": action,
            "note_cn": "home 是启动姿态零位；disable_torque 会让机械臂掉力，请注意托住。",
            "before": summarize_state(arm.get_state()),
        }

        if action == "home":
            command_result = arm.home(
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )
            result["command"] = summarize_motion_result(command_result)
        elif action == "stop":
            result["command"] = summarize_motion_result(arm.stop())
        elif action == "enable_torque":
            arm.enable_torque()
            result["command"] = {"action": "enable_torque", "ok": True}
        elif action == "disable_torque":
            arm.disable_torque()
            result["command"] = {"action": "disable_torque", "ok": True}

        result["after"] = summarize_state(arm.get_state())
        print_json(result)
    finally:
        arm.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    main()
