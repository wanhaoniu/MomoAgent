#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import make_controller, print_json, require_joint_name, summarize_motion_result, summarize_state


def main() -> None:
    # CONFIG_PATH=None 表示使用 SDK 默认配置。
    CONFIG_PATH = None

    # 绝对目标角度：连接机械臂时，当前姿态会被 SDK 记录为 runtime zero。
    # 例如 TARGET_DEG=5.0 表示移动到“相对启动姿态 +5 度”的位置。
    JOINT_NAME = "shoulder_pan"
    TARGET_DEG = 5.0

    # 运动参数：DURATION_SEC=None 时 SDK 会按角度差和 SPEED_PERCENT 自动估计时长。
    DURATION_SEC: float | None = 1.0
    SPEED_PERCENT = 30
    WAIT = True
    TIMEOUT_SEC = 4.0

    # True 时，运动完成后回到脚本刚启动时读到的该关节角度。
    RETURN_TO_START = False

    # 默认不释放扭矩，避免脚本结束后机械臂突然掉力。
    RELEASE_TORQUE_ON_EXIT = False

    joint_name = require_joint_name(JOINT_NAME)
    arm = make_controller(CONFIG_PATH)
    try:
        before_state = arm.get_state()
        start_deg = float(before_state["joint_state"][joint_name])

        result: dict[str, object] = {
            "note_cn": "单关节绝对角度移动；角度参考是本次连接时的启动姿态零点。",
            "joint": joint_name,
            "target_deg": float(TARGET_DEG),
            "start_deg": start_deg,
            "before": summarize_state(before_state),
        }

        move_result = arm.move_joint(
            joint=joint_name,
            target_deg=float(TARGET_DEG),
            duration=DURATION_SEC,
            speed_percent=SPEED_PERCENT,
            wait=bool(WAIT),
            timeout=float(TIMEOUT_SEC),
        )
        result["move"] = summarize_motion_result(move_result)
        result["after_move"] = summarize_state(arm.get_state())

        if RETURN_TO_START:
            return_result = arm.move_joint(
                joint=joint_name,
                target_deg=start_deg,
                duration=DURATION_SEC,
                speed_percent=SPEED_PERCENT,
                wait=bool(WAIT),
                timeout=float(TIMEOUT_SEC),
            )
            result["return_to_start"] = summarize_motion_result(return_result)
            result["after_return"] = summarize_state(arm.get_state())

        print_json(result)
    finally:
        arm.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    main()
