#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import coerce_vector3, make_controller, print_json, summarize_pose_result, summarize_state


def main() -> None:
    CONFIG_PATH = None

    # 绝对末端目标位置，单位是米。建议先用 0_robot_get_state.py 读取当前 tcp_pose，
    # 再在当前 xyz 附近小幅修改。
    TARGET_XYZ_M = [0.20, 0.00, 0.20]

    # 默认只约束位置，减少 IK 失败概率。需要约束姿态时把 USE_ORIENTATION 改成 True。
    USE_ORIENTATION = False
    TARGET_RPY_RAD = [0.0, 0.0, 0.0]
    SEED_POLICY = "current"

    DURATION_SEC: float | None = 1.2
    SPEED_PERCENT = 25
    WAIT = True
    TIMEOUT_SEC = 6.0
    RELEASE_TORQUE_ON_EXIT = False

    target_xyz = coerce_vector3(TARGET_XYZ_M, name="TARGET_XYZ_M")
    target_rpy = coerce_vector3(TARGET_RPY_RAD, name="TARGET_RPY_RAD") if USE_ORIENTATION else None

    arm = make_controller(CONFIG_PATH)
    try:
        before_state = arm.get_state()
        result: dict[str, object] = {
            "note_cn": "笛卡尔绝对位置移动；默认只约束 xyz，不约束末端姿态。",
            "target_xyz_m": target_xyz,
            "target_rpy_rad": target_rpy,
            "seed_policy": SEED_POLICY,
            "before": summarize_state(before_state),
            "before_tcp_pose": before_state["tcp_pose"],
        }
        command_result = arm.move_pose(
            xyz=target_xyz,
            rpy=target_rpy,
            seed_policy=str(SEED_POLICY),
            duration=DURATION_SEC,
            speed_percent=SPEED_PERCENT,
            wait=bool(WAIT),
            timeout=float(TIMEOUT_SEC),
        )
        result["command"] = summarize_pose_result(command_result)
        after_state = arm.get_state()
        result["after"] = summarize_state(after_state)
        result["after_tcp_pose"] = after_state["tcp_pose"]
        print_json(result)
    finally:
        arm.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    main()
