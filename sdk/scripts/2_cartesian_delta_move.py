#!/usr/bin/env python3
from __future__ import annotations

from _robot_script_common import make_controller, print_json, summarize_pose_result, summarize_state


def main() -> None:
    CONFIG_PATH = None

    # 末端相对移动，单位是米和弧度。
    # FRAME="base" 表示沿世界/底座坐标移动；FRAME="tool" 表示沿当前工具坐标移动。
    DX_M = 0.0
    DY_M = 0.0
    DZ_M = 0.01
    DRX_RAD = 0.0
    DRY_RAD = 0.0
    DRZ_RAD = 0.0
    FRAME = "base"

    # None 表示：有旋转增量时约束姿态，没有旋转增量时只约束位置。
    CONSTRAIN_ORIENTATION: bool | None = None

    DURATION_SEC: float | None = 1.0
    SPEED_PERCENT = 25
    WAIT = True
    TIMEOUT_SEC = 5.0
    RELEASE_TORQUE_ON_EXIT = False

    frame = str(FRAME).strip().lower()
    if frame not in {"base", "tool"}:
        raise ValueError(f"FRAME 必须是 'base' 或 'tool'，当前是 {FRAME!r}。")

    arm = make_controller(CONFIG_PATH)
    try:
        before_state = arm.get_state()
        result: dict[str, object] = {
            "note_cn": "笛卡尔增量移动；小步测试时建议从 0.005~0.01 m 开始。",
            "before": summarize_state(before_state),
            "before_tcp_pose": before_state["tcp_pose"],
        }
        command_result = arm.move_delta(
            dx=float(DX_M),
            dy=float(DY_M),
            dz=float(DZ_M),
            drx=float(DRX_RAD),
            dry=float(DRY_RAD),
            drz=float(DRZ_RAD),
            frame=frame,
            duration=DURATION_SEC,
            speed_percent=SPEED_PERCENT,
            wait=bool(WAIT),
            timeout=float(TIMEOUT_SEC),
            constrain_orientation=CONSTRAIN_ORIENTATION,
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
