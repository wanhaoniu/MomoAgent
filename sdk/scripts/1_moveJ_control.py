#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import make_controller, print_json, require_joint_name, summarize_motion_result, summarize_state


def main() -> None:
    CONFIG_PATH = None

    # 多关节绝对角度目标，只写你想动的关节即可。
    # 这些角度都是相对本次连接启动姿态零点的角度，不是舵机出厂零点。
    TARGETS_DEG = {
        "shoulder_pan": 5.0,
        "shoulder_lift": 0.0,
    }

    DURATION_SEC: float | None = 1.5
    SPEED_PERCENT = 30
    WAIT = True
    TIMEOUT_SEC = 5.0

    # True 时运动后调用 home()，回到“脚本连接时的启动姿态零位”。
    RETURN_HOME = False
    RELEASE_TORQUE_ON_EXIT = False

    targets_deg = {require_joint_name(joint): float(value) for joint, value in TARGETS_DEG.items()}
    arm = make_controller(CONFIG_PATH)
    try:
        result: dict[str, object] = {
            "note_cn": "moveJ 多关节同步运动；home 是启动姿态零位，不是硬件出厂零点。",
            "targets_deg": targets_deg,
            "before": summarize_state(arm.get_state()),
        }

        move_result = arm.move_joints(
            targets_deg,
            duration=DURATION_SEC,
            speed_percent=SPEED_PERCENT,
            wait=bool(WAIT),
            timeout=float(TIMEOUT_SEC),
        )
        result["move"] = summarize_motion_result(move_result)
        result["after_move"] = summarize_state(arm.get_state())

        if RETURN_HOME:
            home_result = arm.home(
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )
            result["return_home"] = summarize_motion_result(home_result)
            result["after_home"] = summarize_state(arm.get_state())

        print_json(result)
    finally:
        arm.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    main()
