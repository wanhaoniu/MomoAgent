from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable


# =========================
# 常用可改参数
# =========================

# 可选：自定义配置文件路径。填 None 时，走 SOARMMOCE_CONFIG 或 SDK 默认配置。
CONFIG_PATH: str | None = None

# 运行模式：
# - "compare"      先测单次目标（wait=False），再测 SDK 插值（wait=True）
# - "single_write" 只测单次目标
# - "interpolate"  只测 SDK 插值
TEST_MODE = "compare"

# 要测试的目标关节角度，单位：度。
# 建议一开始先只动 1 个关节，确认安全后再加别的关节。
TARGETS_DEG: dict[str, float] = {
    "shoulder_pan": 30.0,
}

# 可选：先移动到一个固定起始姿态，再开始测试。
# 如果填 None，则直接以当前姿态作为测试起点。
PRESET_START_DEG: dict[str, float] | None = None

# 主运动时长，单位：秒。
# 对 wait=True 的插值运动会直接生效。
# 对 wait=False 的单次写目标，当前 SDK 执行层基本不会按这个时长做插值，
# 这个脚本就是专门帮你验证这一点。
MOVE_DURATION_SEC = 4.0

# 每个阶段开始前是否等你按回车，方便你站到机械臂旁边观察。
PROMPT_BEFORE_PHASE = True

# wait=False 发完指令后，观察多久，单位：秒。
OBSERVE_AFTER_SINGLE_WRITE_SEC = 5.0

# wait=False 观察阶段的状态采样间隔，单位：秒。
OBSERVE_SAMPLE_INTERVAL_SEC = 0.10

# 两个阶段之间是否回到起始姿态，方便做“同起点”的对比。
RETURN_TO_START_BETWEEN_PHASES = True

# 回起点的运动时长，单位：秒。
RETURN_DURATION_SEC = 4.0

# 每个阶段结束后额外停一下，给机械臂一个稳定时间。
SETTLE_PAUSE_SEC = 0.6

# 脚本结束时是否释放力矩。
# False：保持当前位置
# True：退出后尝试松力
RELEASE_TORQUE_ON_EXIT = False

# 是否打印完整 state；默认只打印重点关节，输出更容易看。
PRINT_FULL_STATE = False


def _json_print(title: str, payload: Any) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _now_text() -> str:
    return time.strftime("%H:%M:%S")


def _validate_targets(targets_deg: dict[str, float]) -> None:
    if not targets_deg:
        raise ValueError("TARGETS_DEG 不能为空")
    invalid = [joint_name for joint_name in targets_deg if joint_name not in JOINTS]
    if invalid:
        raise ValueError(f"TARGETS_DEG 里存在未知关节: {invalid}")


def _prompt(message: str) -> None:
    if not PROMPT_BEFORE_PHASE:
        return
    input(f"\n[{_now_text()}] {message} 按回车继续...")


def _focus_joint_names() -> list[str]:
    names = set(TARGETS_DEG)
    if PRESET_START_DEG:
        names.update(PRESET_START_DEG)
    return sorted(names)


def _read_focus_snapshot(controller: SoArmMoceController, joint_names: list[str]) -> dict[str, Any]:
    state = to_jsonable(controller.get_state())
    joint_state = dict(state.get("joint_state", {}))
    relative_raw = dict(state.get("relative_raw_position", {}))
    startup_raw = dict(state.get("startup_raw_position", {}))
    bus = controller._ensure_bus()

    moving_flags: dict[str, Any] = {}
    goal_raw: dict[str, Any] = {}
    present_raw: dict[str, Any] = {}
    for joint_name in joint_names:
        try:
            moving_flags[joint_name] = int(bus.read("Moving", joint_name, normalize=False))
        except Exception as exc:  # noqa: BLE001
            moving_flags[joint_name] = f"{type(exc).__name__}: {exc}"
        try:
            goal_raw[joint_name] = int(bus.read("Goal_Position", joint_name, normalize=False))
        except Exception as exc:  # noqa: BLE001
            goal_raw[joint_name] = f"{type(exc).__name__}: {exc}"
        try:
            present_raw[joint_name] = int(bus.read("Present_Position", joint_name, normalize=False))
        except Exception as exc:  # noqa: BLE001
            present_raw[joint_name] = f"{type(exc).__name__}: {exc}"

    summary = {
        "timestamp": state.get("timestamp"),
        "joint_deg": {joint_name: joint_state.get(joint_name) for joint_name in joint_names},
        "relative_raw": {joint_name: relative_raw.get(joint_name) for joint_name in joint_names},
        "startup_raw": {joint_name: startup_raw.get(joint_name) for joint_name in joint_names},
        "goal_raw": goal_raw,
        "present_raw": present_raw,
        "moving": moving_flags,
    }
    if PRINT_FULL_STATE:
        summary["full_state"] = state
    return summary


def _capture_current_pose(controller: SoArmMoceController, joint_names: list[str]) -> dict[str, float]:
    state = to_jsonable(controller.get_state())
    joint_state = dict(state.get("joint_state", {}))
    return {joint_name: float(joint_state[joint_name]) for joint_name in joint_names}


def _move_and_wait(
    controller: SoArmMoceController,
    *,
    targets_deg: dict[str, float],
    duration_sec: float,
    label: str,
) -> None:
    print(
        f"\n[{_now_text()}] {label}: "
        f"wait=True, duration={float(duration_sec):.2f}s, targets={targets_deg}"
    )
    started = time.perf_counter()
    result = controller.move_joints(
        targets_deg,
        duration=float(duration_sec),
        wait=True,
    )
    elapsed = time.perf_counter() - started
    _json_print(
        f"{label}_result",
        {
            "elapsed_sec": elapsed,
            "targets_deg": dict(result.get("targets_deg", {})),
            "goal_raw": dict(result.get("goal_raw", {})),
        },
    )


def _run_single_write_phase(controller: SoArmMoceController, joint_names: list[str]) -> None:
    _prompt("准备开始单次目标测试（wait=False）")
    _json_print("single_write_before", _read_focus_snapshot(controller, joint_names))

    print(
        f"\n[{_now_text()}] single_write_command: "
        f"wait=False, duration={float(MOVE_DURATION_SEC):.2f}s, targets={TARGETS_DEG}"
    )
    started = time.perf_counter()
    result = controller.move_joints(
        TARGETS_DEG,
        duration=float(MOVE_DURATION_SEC),
        wait=False,
    )
    elapsed = time.perf_counter() - started
    _json_print(
        "single_write_command_result",
        {
            "python_return_elapsed_sec": elapsed,
            "targets_deg": dict(result.get("targets_deg", {})),
            "goal_raw": dict(result.get("goal_raw", {})),
            "说明": "这里返回很快是正常现象，重点观察机械臂实体运动是不是也像插值那样平滑。",
        },
    )

    observe_deadline = time.monotonic() + max(0.0, float(OBSERVE_AFTER_SINGLE_WRITE_SEC))
    sample_index = 0
    while True:
        remaining = max(0.0, observe_deadline - time.monotonic())
        snapshot = _read_focus_snapshot(controller, joint_names)
        snapshot["sample_index"] = sample_index
        snapshot["observe_remaining_sec"] = remaining
        _json_print("single_write_sample", snapshot)
        if remaining <= 0.0:
            break
        sample_index += 1
        time.sleep(max(0.02, float(OBSERVE_SAMPLE_INTERVAL_SEC)))

    time.sleep(max(0.0, float(SETTLE_PAUSE_SEC)))
    _json_print("single_write_after", _read_focus_snapshot(controller, joint_names))


def _run_interpolation_phase(controller: SoArmMoceController, joint_names: list[str]) -> None:
    _prompt("准备开始 SDK 插值测试（wait=True）")
    _json_print("interpolation_before", _read_focus_snapshot(controller, joint_names))
    _move_and_wait(
        controller,
        targets_deg=dict(TARGETS_DEG),
        duration_sec=float(MOVE_DURATION_SEC),
        label="interpolation",
    )
    time.sleep(max(0.0, float(SETTLE_PAUSE_SEC)))
    _json_print("interpolation_after", _read_focus_snapshot(controller, joint_names))


def _maybe_move_to_preset_start(controller: SoArmMoceController, joint_names: list[str]) -> None:
    if not PRESET_START_DEG:
        return
    _prompt("准备先移动到预设起始姿态")
    _move_and_wait(
        controller,
        targets_deg=dict(PRESET_START_DEG),
        duration_sec=float(RETURN_DURATION_SEC),
        label="preset_start",
    )
    time.sleep(max(0.0, float(SETTLE_PAUSE_SEC)))
    _json_print("preset_start_after", _read_focus_snapshot(controller, joint_names))


def _return_to_pose(
    controller: SoArmMoceController,
    *,
    pose_deg: dict[str, float],
    label: str,
) -> None:
    _prompt(f"准备回到{label}")
    _move_and_wait(
        controller,
        targets_deg=dict(pose_deg),
        duration_sec=float(RETURN_DURATION_SEC),
        label=label,
    )
    time.sleep(max(0.0, float(SETTLE_PAUSE_SEC)))


def main() -> int:
    _validate_targets(TARGETS_DEG)
    if TEST_MODE not in {"compare", "single_write", "interpolate"}:
        raise ValueError(f"未知 TEST_MODE: {TEST_MODE}")

    controller = SoArmMoceController(resolve_config(CONFIG_PATH))
    joint_names = _focus_joint_names()

    print("这是一个手动硬件测试脚本，用来对比当前 SDK 的两种路径：")
    print("1. wait=False：单次写目标")
    print("2. wait=True：SDK 软件插值")
    print("请先确认机械臂周围安全，并且目标角度不会撞限位。")

    try:
        controller._ensure_bus()
        _json_print("initial_focus_state", _read_focus_snapshot(controller, joint_names))

        _maybe_move_to_preset_start(controller, joint_names)
        start_pose_deg = _capture_current_pose(controller, joint_names)
        _json_print("captured_start_pose_deg", start_pose_deg)

        if TEST_MODE in {"compare", "single_write"}:
            _run_single_write_phase(controller, joint_names)
            if RETURN_TO_START_BETWEEN_PHASES:
                _return_to_pose(
                    controller,
                    pose_deg=start_pose_deg,
                    label="return_after_single_write",
                )

        if TEST_MODE in {"compare", "interpolate"}:
            _run_interpolation_phase(controller, joint_names)
            if RETURN_TO_START_BETWEEN_PHASES:
                _return_to_pose(
                    controller,
                    pose_deg=start_pose_deg,
                    label="return_after_interpolation",
                )

        _json_print("final_focus_state", _read_focus_snapshot(controller, joint_names))
        print("\n测试结束。建议你重点对比实体观感：")
        print("- wait=False 是否像“瞬间写目标后，舵机自己冲过去”")
        print("- wait=True 是否更接近“软件插值分段发送”的慢速平滑运动")
        return 0
    finally:
        controller.close(disable_torque=bool(RELEASE_TORQUE_ON_EXIT))


if __name__ == "__main__":
    raise SystemExit(main())
