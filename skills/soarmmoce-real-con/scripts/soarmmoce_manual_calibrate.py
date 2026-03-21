#!/usr/bin/env python3
"""Interactive manual calibration for the soarmMoce real arm."""

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import draccus
from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode
from lerobot.utils.utils import enter_pressed, move_cursor_up

from soarmmoce_auto_calibrate import (
    DEFAULT_CONNECT_TIMEOUT_S,
    MultiTurnTrackerState,
    _build_multi_turn_calibration_entry,
    _build_single_turn_calibration_entry,
    _parse_joints,
    _read_joint_snapshot,
    _read_json,
    _single_turn_zero_present_raw,
    _write_json,
)
from soarmmoce_cli_common import run_and_print
from soarmmoce_sdk import JOINTS, MULTI_TURN_JOINTS, SoArmMoceConfig, resolve_config


SUPPORTED_ROBOT_TYPES = {"soarmmoce"}
CALIBRATION_META_KEY = "_meta"


class _ManualConnectTimeout(RuntimeError):
    pass


@dataclass
class ManualCalibrateRobotConfig:
    type: str = "soarmmoce"
    port: str = ""
    id: str = ""
    calib_dir: str = ""
    joints: str = ",".join(JOINTS)
    output: str = ""
    apply_registers: bool = True
    save_json: bool = True
    display_values: bool = True
    poll_interval_s: float = 0.05
    prompt_existing: bool = True

    def __post_init__(self) -> None:
        robot_type = str(self.type or "").strip().lower()
        if robot_type not in SUPPORTED_ROBOT_TYPES:
            supported = ", ".join(sorted(SUPPORTED_ROBOT_TYPES))
            raise ValueError(f"Unsupported robot.type={self.type!r}. Expected one of: {supported}")


@dataclass
class ManualCalibrateConfig:
    robot: ManualCalibrateRobotConfig = field(default_factory=ManualCalibrateRobotConfig)


def _disconnect_bus(bus: FeetechMotorsBus | None) -> None:
    if bus is None:
        return
    disconnect = getattr(bus, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def _connect_manual_calibration_bus(port: str, timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> FeetechMotorsBus:
    bus = FeetechMotorsBus(
        port=port,
        motors={
            "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
            "shoulder_lift": Motor(2, "sts3215", MotorNormMode.DEGREES),
            "elbow_flex": Motor(3, "sts3215", MotorNormMode.DEGREES),
            "wrist_flex": Motor(4, "sts3215", MotorNormMode.DEGREES),
            "wrist_roll": Motor(5, "sts3215", MotorNormMode.DEGREES),
        },
    )

    timeout_s = float(timeout_s)
    previous_handler = None

    if timeout_s > 0.0:
        def _handle_timeout(signum, frame):  # pragma: no cover - signal-driven path
            raise _ManualConnectTimeout(f"Timed out after {timeout_s:.1f}s while connecting to arm bus")

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)

    try:
        bus.connect()
    except _ManualConnectTimeout as exc:
        _disconnect_bus(bus)
        raise RuntimeError(
            f"{exc}. The script is hanging during serial handshake; check SOARMMOCE_PORT, power, and port occupancy."
        ) from exc
    except Exception:
        _disconnect_bus(bus)
        raise
    finally:
        if timeout_s > 0.0:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)

    try:
        # Manual calibration must stay backdrivable from the moment the bus comes up.
        bus.disable_torque()
    except Exception:
        _disconnect_bus(bus)
        raise

    return bus


def _require_modern_multi_turn_calibration(payload: Dict[str, Any], joints: list[str]) -> None:
    missing: list[str] = []
    for joint in joints:
        if joint not in MULTI_TURN_JOINTS:
            continue
        entry = payload.get(joint)
        if not isinstance(entry, dict):
            missing.append(joint)
            continue
        required = {"home_wrapped_raw", "min_relative_raw", "max_relative_raw"}
        if not required.issubset(entry):
            missing.append(joint)
    if missing:
        raise ValueError(
            "The selected calibration JSON still uses the removed multi-turn format. "
            "Re-run calibration for: " + ", ".join(sorted(missing))
        )


def _resolve_runtime_context(robot_cfg: ManualCalibrateRobotConfig) -> Dict[str, Any]:
    base = resolve_config()
    target_port = str(robot_cfg.port or base.port).strip()
    target_robot_id = str(robot_cfg.id or base.robot_id).strip()
    if not target_robot_id:
        raise ValueError("robot.id must not be empty")

    if robot_cfg.calib_dir:
        target_calib_dir = Path(robot_cfg.calib_dir).expanduser().resolve()
    else:
        target_calib_dir = Path(base.calib_dir).resolve()

    target_source_path = (target_calib_dir / f"{target_robot_id}.json").resolve()
    if robot_cfg.output:
        output_path = Path(robot_cfg.output).expanduser().resolve()
    else:
        output_path = target_source_path

    runtime_candidates = [
        target_source_path,
        (target_calib_dir / f"{base.robot_id}.json").resolve(),
        (Path(base.calib_dir).resolve() / f"{target_robot_id}.json").resolve(),
        (Path(base.calib_dir).resolve() / f"{base.robot_id}.json").resolve(),
    ]

    runtime_calibration_path = None
    runtime_robot_id = target_robot_id
    runtime_calib_dir = target_calib_dir
    for candidate in runtime_candidates:
        if candidate.exists():
            runtime_calibration_path = candidate
            runtime_robot_id = candidate.stem
            runtime_calib_dir = candidate.parent
            break

    if runtime_calibration_path is None:
        raise FileNotFoundError(
            "No calibration JSON is available to bootstrap the controller. Tried: "
            + ", ".join(str(path) for path in runtime_candidates)
        )

    runtime_config = SoArmMoceConfig(
        port=target_port,
        robot_id=runtime_robot_id,
        calib_dir=runtime_calib_dir,
        urdf_path=base.urdf_path,
        runtime_dir=base.runtime_dir,
        target_frame=base.target_frame,
        home_joints=dict(base.home_joints),
        joint_scales=dict(base.joint_scales),
        model_offsets_deg=dict(base.model_offsets_deg),
        arm_p_coefficient=base.arm_p_coefficient,
        arm_d_coefficient=base.arm_d_coefficient,
        max_ee_pos_err_m=base.max_ee_pos_err_m,
        linear_step_m=base.linear_step_m,
        joint_step_deg=base.joint_step_deg,
        cartesian_settle_time_s=base.cartesian_settle_time_s,
        cartesian_update_hz=base.cartesian_update_hz,
        joint_update_hz=base.joint_update_hz,
        ik_target_tol_m=base.ik_target_tol_m,
        ik_max_iters=base.ik_max_iters,
        ik_damping=base.ik_damping,
        ik_step_scale=base.ik_step_scale,
        ik_joint_step_deg=base.ik_joint_step_deg,
        ik_seed_bias=base.ik_seed_bias,
    )

    return {
        "runtime_config": runtime_config,
        "runtime_calibration_path": runtime_calibration_path,
        "target_robot_id": target_robot_id,
        "target_calib_dir": target_calib_dir,
        "target_source_path": target_source_path,
        "output_path": output_path,
    }


def _capture_home_pose(
    *,
    bus,
    joints: list[str],
    raw_offset_correction: Dict[str, int],
    poll_interval_s: float,
    display_values: bool,
) -> tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, MultiTurnTrackerState]]:
    tracker: Dict[str, MultiTurnTrackerState] = {}
    home_present_raw: Dict[str, int] = {}
    home_present_wrapped_raw: Dict[str, int] = {}
    desired_home_raw: Dict[str, int] = {}
    captured = False

    while not captured:
        for joint in joints:
            snap = _read_joint_snapshot(
                bus,
                joint,
                tracker=tracker,
                homing_offset_raw=int(raw_offset_correction.get(joint, 0)),
            )
            home_present_raw[joint] = int(snap["position"])
            home_present_wrapped_raw[joint] = int(snap["position_wrapped"])
            if joint in MULTI_TURN_JOINTS:
                desired_home_raw[joint] = 0
            else:
                model = bus.motors[joint].model
                max_res = int(bus.model_resolution_table[model] - 1)
                desired_home_raw[joint] = int(_single_turn_zero_present_raw(max_res))

        if display_values:
            print(
                "\nThis step captures zero references only. Joint limits are recorded in the next step."
            )
            print(
                f"{'NAME':<15} | {'ACTUAL_RAW':>10} | {'WRAPPED':>8} | {'ZERO_REF':>10} | {'MODE':>12}"
            )
            for joint in joints:
                mode = "soft_zero" if joint in MULTI_TURN_JOINTS else "half_turn"
                zero_ref = "current" if joint in MULTI_TURN_JOINTS else str(desired_home_raw[joint])
                print(
                    f"{joint:<15} | {home_present_raw[joint]:>10} | "
                    f"{home_present_wrapped_raw[joint]:>8} | {zero_ref:>10} | {mode:>12}"
                )

        if enter_pressed():
            captured = True
        else:
            time.sleep(max(0.01, float(poll_interval_s)))
            if display_values:
                move_cursor_up(len(joints) + 3)

    return home_present_raw, home_present_wrapped_raw, desired_home_raw, tracker


def _prepare_multi_turn_joints_for_calibration(bus, joints: list[str]) -> None:
    for joint in joints:
        model = bus.motors[joint].model
        max_res = int(bus.model_resolution_table[model] - 1)
        bus.write("Homing_Offset", joint, 0, normalize=False)
        bus.write("Min_Position_Limit", joint, 0, normalize=False)
        bus.write("Max_Position_Limit", joint, max_res, normalize=False)
        bus.write("Operating_Mode", joint, OperatingMode.POSITION.value, normalize=False)
    if joints:
        time.sleep(0.05)


def _record_manual_ranges(
    *,
    bus,
    joints: list[str],
    raw_offset_correction: Dict[str, int],
    poll_interval_s: float,
    display_values: bool,
    tracker: Dict[str, MultiTurnTrackerState] | None = None,
) -> tuple[Dict[str, int], Dict[str, int]]:
    if not joints:
        return {}, {}

    positions = {
        joint: int(
            _read_joint_snapshot(
                bus,
                joint,
                tracker=tracker,
                homing_offset_raw=int(raw_offset_correction.get(joint, 0)),
            )["position"]
        )
        for joint in joints
    }
    mins = dict(positions)
    maxes = dict(positions)
    user_pressed_enter = False

    while not user_pressed_enter:
        positions = {
            joint: int(
                _read_joint_snapshot(
                    bus,
                    joint,
                    tracker=tracker,
                    homing_offset_raw=int(raw_offset_correction.get(joint, 0)),
                )["position"]
            )
            for joint in joints
        }
        mins = {joint: min(mins[joint], positions[joint]) for joint in joints}
        maxes = {joint: max(maxes[joint], positions[joint]) for joint in joints}

        if display_values:
            print("\n-------------------------------------------")
            print(f"{'NAME':<15} | {'MIN':>6} | {'POS':>6} | {'MAX':>6}")
            for joint in joints:
                print(f"{joint:<15} | {mins[joint]:>6} | {positions[joint]:>6} | {maxes[joint]:>6}")

        if enter_pressed():
            user_pressed_enter = True
        else:
            time.sleep(max(0.01, float(poll_interval_s)))
            if display_values:
                move_cursor_up(len(joints) + 3)

    same_min_max = [joint for joint in joints if mins[joint] == maxes[joint]]
    if same_min_max:
        raise ValueError(
            "Some joints did not move during manual range recording: "
            + ", ".join(sorted(same_min_max))
        )

    return mins, maxes


def _apply_selected_calibration(bus, joints: list[str], payload: Dict[str, Any]) -> None:
    for joint in joints:
        entry = payload.get(joint)
        if not isinstance(entry, dict):
            raise KeyError(f"Calibration entry for {joint} is missing from the selected calibration payload")
        bus.write("Homing_Offset", joint, int(entry["homing_offset"]), normalize=False)
        bus.write("Min_Position_Limit", joint, int(entry["range_min"]), normalize=False)
        bus.write("Max_Position_Limit", joint, int(entry["range_max"]), normalize=False)


def _confirm_multi_turn_home_capture(
    *,
    joints: list[str],
    home_present_raw: Dict[str, int],
) -> None:
    if not joints:
        return

    print(
        "The CURRENT pose will become software zero for the selected multi-turn joints. "
        "You will record the full range in the next step."
    )
    print(f"{'NAME':<15} | {'CAPTURED_RAW':>12} | {'HOME_ZERO':>12}")
    for joint in joints:
        print(f"{joint:<15} | {int(home_present_raw[joint]):>12} | {0:>12}")

    user_input = input("Type 'ok' and press ENTER to continue, or just press ENTER to abort: ")
    if user_input.strip().lower() != "ok":
        raise RuntimeError("Manual calibration aborted before accepting the captured multi-turn home pose")


def _manual_calibrate(cfg: ManualCalibrateConfig) -> Dict[str, Any]:
    robot_cfg = cfg.robot
    joints = list(dict.fromkeys(_parse_joints(robot_cfg.joints)))
    single_turn_joints = [joint for joint in joints if joint not in MULTI_TURN_JOINTS]
    multi_turn_joints = [joint for joint in joints if joint in MULTI_TURN_JOINTS]
    context = _resolve_runtime_context(robot_cfg)
    runtime_config = context["runtime_config"]
    target_source_path = context["target_source_path"]
    output_path = context["output_path"]
    runtime_calibration_path = context["runtime_calibration_path"]
    target_robot_id = str(context["target_robot_id"])

    target_calib_json = _read_json(target_source_path)
    seed_calib_json = target_calib_json or _read_json(runtime_calibration_path)
    calibration_seed_path = target_source_path if target_calib_json else runtime_calibration_path

    bus = _connect_manual_calibration_bus(runtime_config.port)
    try:
        if target_calib_json and bool(robot_cfg.prompt_existing):
            user_input = input(
                f"Press ENTER to use the calibration file associated with the id {target_robot_id}, "
                "or type 'c' and press ENTER to run manual calibration: "
            )
            if user_input.strip().lower() != "c":
                _require_modern_multi_turn_calibration(target_calib_json, joints)
                if robot_cfg.apply_registers:
                    bus.disable_torque()
                    _apply_selected_calibration(bus, joints, target_calib_json)
                if robot_cfg.save_json and output_path != target_source_path:
                    _write_json(output_path, target_calib_json)
                return {
                    "action": "manual_calibrate",
                    "mode": "use_existing_calibration",
                    "robot_id": target_robot_id,
                    "port": runtime_config.port,
                    "joints": joints,
                    "source_calibration_path": str(target_source_path),
                    "output_path": str(output_path),
                    "saved_json": bool(robot_cfg.save_json and output_path != target_source_path),
                    "applied_registers": bool(robot_cfg.apply_registers),
                }

        current_hw_calib = bus.read_calibration()
        home_present_raw: Dict[str, int] = {}
        home_present_wrapped_raw: Dict[str, int] = {}
        min_present_raw: Dict[str, int] = {}
        max_present_raw: Dict[str, int] = {}
        desired_home_raw: Dict[str, int] = {}
        written_json = dict(seed_calib_json) if isinstance(seed_calib_json, dict) else {}
        register_writes: Dict[str, Dict[str, Any]] = {}
        results: Dict[str, Dict[str, Any]] = {}
        tracker: Dict[str, MultiTurnTrackerState] = {}
        raw_offset_correction = {joint: int(current_hw_calib[joint].homing_offset) for joint in joints}
        single_turn_homing_offsets: Dict[str, int] = {}

        _prepare_multi_turn_joints_for_calibration(bus, multi_turn_joints)
        for joint in multi_turn_joints:
            raw_offset_correction[joint] = 0

        print(
            "Torque is disabled. Place single-turn joints at the URDF q=0 pose. "
            "For multi-turn joints, the current pose becomes software zero. "
            "Press ENTER to capture the current pose."
        )
        home_present_raw, home_present_wrapped_raw, desired_home_raw, tracker = _capture_home_pose(
            bus=bus,
            joints=joints,
            raw_offset_correction=raw_offset_correction,
            poll_interval_s=float(robot_cfg.poll_interval_s),
            display_values=bool(robot_cfg.display_values),
        )

        _confirm_multi_turn_home_capture(
            joints=multi_turn_joints,
            home_present_raw=home_present_raw,
        )

        if single_turn_joints:
            offsets = bus.set_half_turn_homings(single_turn_joints)
            single_turn_homing_offsets = {str(joint): int(offset) for joint, offset in offsets.items()}

        if joints:
            print(
                "Move the selected joints sequentially through their entire ranges of motion.\n"
                "Single-turn joints are re-centered to 2047 before recording.\n"
                "Multi-turn joints are tracked continuously from the captured home reference.\n"
                "Recording positions with torque disabled. Press ENTER to stop..."
            )
            min_present_raw, max_present_raw = _record_manual_ranges(
                bus=bus,
                joints=joints,
                raw_offset_correction={joint: 0 for joint in joints},
                poll_interval_s=float(robot_cfg.poll_interval_s),
                display_values=bool(robot_cfg.display_values),
                tracker=tracker,
            )

        for joint in joints:
            current_cal = current_hw_calib[joint]

            if joint in MULTI_TURN_JOINTS:
                entry, result_payload = _build_multi_turn_calibration_entry(
                    current_cal=current_cal,
                    home_present_raw=int(home_present_raw[joint]),
                    home_present_wrapped_raw=int(home_present_wrapped_raw[joint]),
                    min_present_raw=int(min_present_raw[joint]),
                    max_present_raw=int(max_present_raw[joint]),
                )
                written_json[joint] = entry
                results[joint] = result_payload
                register_writes[joint] = {
                    "homing_offset": int(entry["homing_offset"]),
                    "range_min": int(entry["range_min"]),
                    "range_max": int(entry["range_max"]),
                    "home_wrapped_raw": int(entry["home_wrapped_raw"]),
                    "min_relative_raw": int(entry["min_relative_raw"]),
                    "max_relative_raw": int(entry["max_relative_raw"]),
                    "calibration_mode": str(result_payload["calibration_mode"]),
                }
            else:
                model = bus.motors[joint].model
                max_res = int(bus.model_resolution_table[model] - 1)
                entry, result_payload = _build_single_turn_calibration_entry(
                    current_cal=current_cal,
                    max_res=max_res,
                    homing_offset_raw=int(single_turn_homing_offsets[joint]),
                    min_present_raw=int(min_present_raw[joint]),
                    max_present_raw=int(max_present_raw[joint]),
                )
                results[joint] = {
                    **result_payload,
                    "captured_zero_raw_before_half_turn": int(home_present_raw[joint]),
                    "home_present_wrapped_raw": int(home_present_wrapped_raw[joint]),
                }
                written_json[joint] = entry
                register_writes[joint] = {
                    "homing_offset": int(entry["homing_offset"]),
                    "range_min": int(entry["range_min"]),
                    "range_max": int(entry["range_max"]),
                    "zero_present_raw": int(result_payload["zero_present_raw"]),
                    "calibration_mode": str(result_payload["calibration_mode"]),
                }

        if robot_cfg.apply_registers:
            bus.disable_torque()
            _apply_selected_calibration(bus, joints, written_json)

        if robot_cfg.save_json:
            written_json[CALIBRATION_META_KEY] = {
                "home_joint_deg": {joint: 0.0 for joint in JOINTS},
            }
            _write_json(output_path, written_json)

        return {
            "action": "manual_calibrate",
            "mode": "interactive_manual",
            "robot_id": target_robot_id,
            "port": runtime_config.port,
            "joints": joints,
            "single_turn_joints": single_turn_joints,
            "multi_turn_joints": multi_turn_joints,
            "runtime_calibration_path": str(runtime_calibration_path),
            "calibration_seed_path": str(calibration_seed_path),
            "output_path": str(output_path),
            "saved_json": bool(robot_cfg.save_json),
            "applied_registers": bool(robot_cfg.apply_registers),
            "home_reference_note": "single-turn joints use the URDF q=0 pose; multi-turn joints use the captured pose as software zero",
            "single_turn_note": "single-turn joints use the LeRobot half-turn zero plus manual range recording with torque disabled",
            "multi_turn_note": "multi-turn joints store home_wrapped_raw plus relative continuous raw limits recorded with software unwrap in position mode",
            "results": results,
            "register_writes": register_writes,
        }
    finally:
        _disconnect_bus(bus)


@draccus.wrap()
def main(cfg: ManualCalibrateConfig) -> None:
    run_and_print(lambda: _manual_calibrate(cfg))


if __name__ == "__main__":
    main()
