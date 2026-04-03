from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SRC = REPO_ROOT / "sdk" / "src"
SKILL_SCRIPTS = REPO_ROOT / "skills" / "soarmmoce-real-con" / "scripts"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SKILL_SCRIPTS))

from soarmmoce_sdk import (
    JOINTS,
    MULTI_TURN_ABSOLUTE_RAW_LIMIT,
    MULTI_TURN_DISABLED_LIMIT_RAW,
    MULTI_TURN_JOINTS,
    MULTI_TURN_PHASE_VALUE,
    SoArmMoceConfig,
    SoArmMoceController,
)
from soarmmoce_sdk.real_arm import HardwareError, POSITION_MODE_VALUE, SINGLE_TURN_RAW_MAX, SINGLE_TURN_RAW_MIN

try:
    from soarmmoce_calibrate import _build_multi_turn_calibration_entry, _command_goal_from_reference, _read_joint_snapshot
except ImportError:
    def _wrap_position_raw(raw_value: int | float) -> int:
        return int(raw_value) % 4096

    def _read_joint_snapshot(bus, joint: str, tracker=None, homing_offset_raw: int = 0):
        register_raw = int(bus.read("Present_Position", joint, normalize=False))
        if joint in MULTI_TURN_JOINTS:
            position_key = int(register_raw + int(homing_offset_raw))
            wrapped_raw = _wrap_position_raw(position_key)
        else:
            wrapped_raw = _wrap_position_raw(register_raw + int(homing_offset_raw))
            position_key = int(wrapped_raw)
        return {
            "position": int(position_key),
            "position_wrapped": int(wrapped_raw),
            "position_register_raw": int(register_raw),
            "velocity": float(bus.read("Present_Velocity", joint, normalize=False)),
            "moving": int(bus.read("Moving", joint, normalize=False)),
            "current": float(bus.read("Present_Current", joint, normalize=False)),
        }

    def _command_goal_from_reference(bus, joint: str, direction: int, step_raw: int, reference_position_raw: int):
        direction = 1 if direction >= 0 else -1
        step_raw = max(1, int(step_raw))
        if joint in MULTI_TURN_JOINTS:
            goal_value = int(reference_position_raw) + direction * step_raw
        else:
            goal_value = int(min(4095, max(0, int(reference_position_raw) + direction * step_raw)))
        bus.write("Goal_Position", joint, goal_value, normalize=False)
        return {
            "kind": "absolute_goal",
            "goal_value": int(goal_value),
            "from_position": int(reference_position_raw),
        }

    def _build_multi_turn_calibration_entry(
        *,
        current_cal,
        home_present_raw: int,
        home_present_wrapped_raw: int,
        min_present_raw: int,
        max_present_raw: int,
    ):
        entry = {
            "id": int(current_cal.id),
            "drive_mode": int(getattr(current_cal, "drive_mode", 0)),
            "homing_offset": 0,
            "phase": MULTI_TURN_PHASE_VALUE,
            "range_min": MULTI_TURN_DISABLED_LIMIT_RAW,
            "range_max": MULTI_TURN_DISABLED_LIMIT_RAW,
            "operating_mode": 0,
            "home_present_raw": int(home_present_raw),
            "home_wrapped_raw": int(home_present_wrapped_raw),
        }
        payload = {
            "calibration_mode": "multi_turn_mode0_absolute_position",
            "home_present_raw": int(home_present_raw),
            "home_present_wrapped_raw": int(home_present_wrapped_raw),
            "min_present_raw": int(min_present_raw),
            "max_present_raw": int(max_present_raw),
        }
        return entry, payload


CALIB_DIR = REPO_ROOT / "skills" / "soarmmoce-real-con" / "calibration"


def _make_controller() -> SoArmMoceController:
    return SoArmMoceController(
        SoArmMoceConfig(
            port="",
            robot_id="soarmmoce",
            calib_dir=CALIB_DIR,
            joint_scales={name: 1.0 for name in JOINTS},
            model_offsets_deg={name: 0.0 for name in JOINTS},
        )
    )


class _FakeReadBus:
    def __init__(self, position: int):
        self.position = int(position)

    def read(self, data_name: str, joint: str, normalize: bool = False):
        assert normalize is False
        if data_name == "Present_Position":
            return self.position
        if data_name == "Present_Velocity":
            return 0
        if data_name == "Moving":
            return 0
        if data_name == "Present_Current":
            return 0
        raise AssertionError(f"Unexpected read: {data_name} {joint}")


class _FakePrimeBus:
    def __init__(self, positions: dict[str, int]):
        self.positions = {name: int(value) for name, value in positions.items()}

    def sync_read(self, data_name: str, normalize: bool = False):
        assert data_name == "Present_Position"
        assert normalize is False
        return {name: self.positions[name] for name in JOINTS}

    def read(self, data_name: str, joint: str, normalize: bool = False):
        assert data_name == "Present_Position"
        assert normalize is False
        return self.positions[joint]


class _FakeWriteBus:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, int, bool]] = []

    def write(self, data_name: str, joint: str, value: int, normalize: bool = False) -> None:
        self.writes.append((data_name, joint, int(value), bool(normalize)))

    def sync_write(self, data_name: str, values) -> None:
        raise AssertionError(f"sync_write should not be used in this test: {data_name} {values}")


class _FakeMotionBus:
    def __init__(self, positions: dict[str, int]) -> None:
        self.positions = {name: int(value) for name, value in positions.items()}
        self.writes: list[tuple[str, str, int, bool]] = []

    def sync_read(self, data_name: str, normalize: bool = False):
        assert data_name == "Present_Position"
        assert normalize is False
        return {name: self.positions[name] for name in JOINTS}

    def read(self, data_name: str, joint: str, normalize: bool = False):
        assert normalize is False
        if data_name == "Present_Position":
            return self.positions[joint]
        if data_name == "Moving":
            return 0
        if data_name == "Present_Velocity":
            return 0
        if data_name == "Present_Current":
            return 0
        raise AssertionError(f"Unexpected read: {data_name} {joint}")

    def write(self, data_name: str, joint: str, value: int, normalize: bool = False) -> None:
        self.writes.append((data_name, joint, int(value), bool(normalize)))
        if data_name == "Goal_Position":
            self.positions[joint] = int(value)


class MultiTurnAbsoluteModeTests(unittest.TestCase):
    def test_prime_startup_reference_uses_current_pose_for_multi_turn(self) -> None:
        controller = _make_controller()
        bus = _FakePrimeBus(
            {
                "shoulder_pan": 2047,
                "shoulder_lift": 1512,
                "elbow_flex": -1800,
                "wrist_flex": 2047,
                "wrist_roll": 2047,
            }
        )

        controller._prime_startup_references_from_current_pose(bus)
        state = controller._build_state(bus.positions)

        self.assertEqual(controller._joint_runtime_state["shoulder_lift"].startup_raw, 1512)
        self.assertEqual(controller._joint_runtime_state["elbow_flex"].startup_raw, -1800)
        self.assertEqual(state["relative_raw_position"]["shoulder_lift"], 0)
        self.assertEqual(state["relative_raw_position"]["elbow_flex"], 0)
        self.assertAlmostEqual(state["joint_state"]["shoulder_lift"], 0.0)
        self.assertAlmostEqual(state["joint_state"]["elbow_flex"], 0.0)

    def test_multi_turn_raw_to_joint_deg_uses_startup_reference(self) -> None:
        controller = _make_controller()
        controller._prime_startup_references_from_current_pose(
            _FakePrimeBus(
                {
                    "shoulder_pan": 0,
                    "shoulder_lift": 1000,
                    "elbow_flex": 0,
                    "wrist_flex": 0,
                    "wrist_roll": 0,
                }
            )
        )

        joint_deg = controller._multi_turn_raw_to_joint_deg("shoulder_lift", 1512.0)

        self.assertAlmostEqual(joint_deg, 45.0)
        self.assertEqual(controller._joint_runtime_state["shoulder_lift"].startup_raw, 1000)

    def test_multi_turn_goal_encoding_uses_startup_reference(self) -> None:
        controller = _make_controller()
        controller._prime_startup_references_from_current_pose(
            _FakePrimeBus(
                {
                    "shoulder_pan": 0,
                    "shoulder_lift": 1000,
                    "elbow_flex": 0,
                    "wrist_flex": 0,
                    "wrist_roll": 0,
                }
            )
        )

        self.assertEqual(controller._continuous_raw_to_multi_turn_goal_raw("shoulder_lift", 512.0), 1512)
        self.assertEqual(controller._joint_deg_to_multi_turn_goal_raw("shoulder_lift", 90.0), 2024)

    def test_multi_turn_goal_encoding_rejects_values_outside_absolute_mode_range(self) -> None:
        controller = _make_controller()
        controller._prime_startup_references_from_current_pose(
            _FakePrimeBus(
                {
                    "shoulder_pan": 0,
                    "shoulder_lift": MULTI_TURN_ABSOLUTE_RAW_LIMIT - 10,
                    "elbow_flex": 0,
                    "wrist_flex": 0,
                    "wrist_roll": 0,
                }
            )
        )

        with self.assertRaises(HardwareError):
            controller._continuous_raw_to_multi_turn_goal_raw("shoulder_lift", 32.0)

    def test_single_turn_wrap_is_relative_to_startup_position(self) -> None:
        controller = _make_controller()
        controller._prime_startup_references_from_current_pose(
            _FakePrimeBus(
                {
                    "shoulder_pan": 4090,
                    "shoulder_lift": 0,
                    "elbow_flex": 0,
                    "wrist_flex": 0,
                    "wrist_roll": 0,
                }
            )
        )

        state = controller._build_state(
            {
                "shoulder_pan": 10,
                "shoulder_lift": 0,
                "elbow_flex": 0,
                "wrist_flex": 0,
                "wrist_roll": 0,
            }
        )

        self.assertEqual(state["relative_raw_position"]["shoulder_pan"], 16)
        self.assertAlmostEqual(state["joint_state"]["shoulder_pan"], 16.0 * 360.0 / 4096.0)

    def test_position_mode_register_writes_follow_single_and_multi_turn_rules(self) -> None:
        controller = _make_controller()
        bus = _FakeWriteBus()

        controller._apply_position_mode_registers(bus)

        writes = {(register, joint): value for register, joint, value, _ in bus.writes}
        shoulder_pan_entry = controller._calibration_payload["shoulder_pan"]

        self.assertEqual(writes[("Operating_Mode", "shoulder_pan")], 0)
        self.assertEqual(writes[("Homing_Offset", "shoulder_pan")], int(shoulder_pan_entry["homing_offset"]))
        self.assertEqual(writes[("Min_Position_Limit", "shoulder_pan")], int(shoulder_pan_entry["range_min"]))
        self.assertEqual(writes[("Max_Position_Limit", "shoulder_pan")], int(shoulder_pan_entry["range_max"]))

        for joint_name in MULTI_TURN_JOINTS:
            self.assertEqual(writes[("Operating_Mode", joint_name)], 0)
            self.assertEqual(writes[("Homing_Offset", joint_name)], 0)
            self.assertEqual(writes[("Min_Position_Limit", joint_name)], MULTI_TURN_DISABLED_LIMIT_RAW)
            self.assertEqual(writes[("Max_Position_Limit", joint_name)], MULTI_TURN_DISABLED_LIMIT_RAW)
            self.assertEqual(writes[("Phase", joint_name)], MULTI_TURN_PHASE_VALUE)

    def test_build_raw_hold_command_returns_current_raw_register_values(self) -> None:
        controller = _make_controller()
        bus = _FakePrimeBus(
            {
                "shoulder_pan": 123,
                "shoulder_lift": 4372,
                "elbow_flex": -2408,
                "wrist_flex": 456,
                "wrist_roll": 789,
            }
        )

        hold_cmd = controller._build_raw_hold_command(bus)

        self.assertEqual(
            hold_cmd,
            {
                "shoulder_pan": 123,
                "shoulder_lift": 4372,
                "elbow_flex": -2408,
                "wrist_flex": 456,
                "wrist_roll": 789,
            },
        )

    def test_write_raw_goal_positions_uses_non_normalized_register_writes(self) -> None:
        controller = _make_controller()
        bus = _FakeWriteBus()

        controller._write_raw_goal_positions(
            bus,
            {
                "shoulder_pan": 101,
                "shoulder_lift": -472,
                "elbow_flex": -6428,
            },
        )

        self.assertEqual(
            bus.writes,
            [
                ("Goal_Position", "shoulder_pan", 101, False),
                ("Goal_Position", "shoulder_lift", -472, False),
                ("Goal_Position", "elbow_flex", -6428, False),
            ],
        )

    def test_auto_calibration_reads_multi_turn_present_position_as_absolute_raw(self) -> None:
        snapshot = _read_joint_snapshot(_FakeReadBus(position=-5000), "shoulder_lift")

        self.assertEqual(snapshot["position"], -5000)
        self.assertEqual(snapshot["position_wrapped"], 3192)
        self.assertEqual(snapshot["position_register_raw"], -5000)

    def test_auto_calibration_writes_absolute_mode_goal_and_metadata(self) -> None:
        bus = _FakeWriteBus()
        info = _command_goal_from_reference(
            bus,
            "shoulder_lift",
            direction=1,
            step_raw=64,
            reference_position_raw=-5000,
        )

        self.assertEqual(
            info,
            {
                "kind": "absolute_goal",
                "goal_value": -4936,
                "from_position": -5000,
            },
        )
        self.assertEqual(bus.writes, [("Goal_Position", "shoulder_lift", -4936, False)])

        entry, payload = _build_multi_turn_calibration_entry(
            current_cal=type("CurrentCal", (), {"id": 2, "drive_mode": 0})(),
            home_present_raw=-4936,
            home_present_wrapped_raw=3256,
            min_present_raw=-9000,
            max_present_raw=-1000,
        )

        self.assertEqual(entry["phase"], MULTI_TURN_PHASE_VALUE)
        self.assertEqual(entry["range_min"], MULTI_TURN_DISABLED_LIMIT_RAW)
        self.assertEqual(entry["range_max"], MULTI_TURN_DISABLED_LIMIT_RAW)
        self.assertEqual(entry["home_present_raw"], -4936)
        self.assertEqual(payload["calibration_mode"], "multi_turn_mode0_absolute_position")

    def test_move_joints_respects_duration_with_intermediate_raw_steps_when_waiting(self) -> None:
        controller = _make_controller()
        bus = _FakeMotionBus(
            {
                "shoulder_pan": 0,
                "shoulder_lift": 0,
                "elbow_flex": 0,
                "wrist_flex": 0,
                "wrist_roll": 0,
            }
        )
        controller._bus = bus
        controller._prime_startup_references_from_current_pose(bus)

        with mock.patch("soarmmoce_sdk.real_arm.time.sleep", return_value=None):
            controller.move_joints({"shoulder_pan": 90.0}, duration=0.2, wait=True)

        goal_writes = [entry for entry in bus.writes if entry[0] == "Goal_Position" and entry[1] == "shoulder_pan"]
        self.assertGreaterEqual(len(goal_writes), 5)

    def test_move_joints_without_wait_keeps_single_raw_write(self) -> None:
        controller = _make_controller()
        bus = _FakeMotionBus(
            {
                "shoulder_pan": 0,
                "shoulder_lift": 0,
                "elbow_flex": 0,
                "wrist_flex": 0,
                "wrist_roll": 0,
            }
        )
        controller._bus = bus
        controller._prime_startup_references_from_current_pose(bus)

        with mock.patch("soarmmoce_sdk.real_arm.time.sleep", return_value=None):
            controller.move_joints({"shoulder_pan": 90.0}, duration=0.2, wait=False)

        goal_writes = [entry for entry in bus.writes if entry[0] == "Goal_Position" and entry[1] == "shoulder_pan"]
        self.assertEqual(len(goal_writes), 1)

    def test_capture_hold_state_includes_gripper_when_integrated(self) -> None:
        controller = _make_controller()
        bus = _FakeMotionBus(
            {
                "shoulder_pan": 123,
                "shoulder_lift": 4372,
                "elbow_flex": -2408,
                "wrist_flex": 456,
                "wrist_roll": 789,
                "gripper": 4014,
            }
        )
        controller._bus = bus
        controller._gripper_integrated = True

        hold_state = controller.capture_hold_state(bus)

        self.assertEqual(hold_state["gripper_goal_raw"], 4014)
        self.assertEqual(hold_state["joint_goal_raw"]["shoulder_lift"], 4372)

    def test_get_gripper_state_reports_register_and_adjusted_raw(self) -> None:
        controller = _make_controller()
        bus = _FakeMotionBus(
            {
                "shoulder_pan": 0,
                "shoulder_lift": 0,
                "elbow_flex": 0,
                "wrist_flex": 0,
                "wrist_roll": 0,
                "gripper": 4014,
            }
        )
        controller._bus = bus
        controller._gripper_integrated = True

        state = controller.get_gripper_state()
        spec = controller._gripper_spec
        assert spec is not None
        expected_adjusted_raw = (4014 + int(spec.homing_offset)) % 4096
        expected_open_ratio = (expected_adjusted_raw - int(spec.range_min)) / float(spec.range_max - spec.range_min)

        self.assertIsNotNone(state)
        self.assertEqual(state["present_raw"], 4014)
        self.assertEqual(state["adjusted_raw"], expected_adjusted_raw)
        self.assertAlmostEqual(state["open_ratio"], expected_open_ratio)

    def test_set_gripper_writes_integrated_goal_raw(self) -> None:
        controller = _make_controller()
        bus = _FakeMotionBus(
            {
                "shoulder_pan": 0,
                "shoulder_lift": 0,
                "elbow_flex": 0,
                "wrist_flex": 0,
                "wrist_roll": 0,
                "gripper": 4014,
            }
        )
        controller._bus = bus
        controller._gripper_integrated = True
        controller._prime_startup_references_from_current_pose(bus)

        result = controller.set_gripper(open_ratio=1.0, wait=False)
        spec = controller._gripper_spec
        assert spec is not None
        expected_goal_raw = (int(spec.range_max) - int(spec.homing_offset)) % 4096

        self.assertEqual(result["goal_raw"], expected_goal_raw)
        self.assertEqual(bus.positions["gripper"], expected_goal_raw)
        self.assertIn(("Goal_Position", "gripper", expected_goal_raw, False), bus.writes)

    def test_integrated_gripper_registers_use_full_single_turn_limits_for_raw_replay(self) -> None:
        controller = _make_controller()
        bus = _FakeWriteBus()
        controller._gripper_integrated = True

        controller._apply_gripper_registers(bus)

        writes = {(register, joint): value for register, joint, value, _ in bus.writes}
        self.assertEqual(writes[("Operating_Mode", "gripper")], POSITION_MODE_VALUE)
        self.assertEqual(writes[("Homing_Offset", "gripper")], -1966)
        self.assertEqual(writes[("Min_Position_Limit", "gripper")], SINGLE_TURN_RAW_MIN)
        self.assertEqual(writes[("Max_Position_Limit", "gripper")], SINGLE_TURN_RAW_MAX)


if __name__ == "__main__":
    unittest.main()
