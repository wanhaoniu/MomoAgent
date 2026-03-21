#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
import types
from pathlib import Path

import draccus
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode


JOINT_IDS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
    "gripper": 6,
}
ARM_JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
]
MULTI_TURN_JOINTS = {"shoulder_lift", "elbow_flex"}
DEFAULT_FOLLOWER_CALIBRATION = Path(__file__).resolve().parent / "calibration" / "white_arm_follower.json"
MULTI_TURN_RAW_RANGE = 900000


def load_calibration(path: Path) -> dict[str, MotorCalibration]:
    with path.open("r", encoding="utf-8") as handle, draccus.config_type("json"):
        return draccus.load(dict[str, MotorCalibration], handle)


def send_socket_opts(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def recv_json_line(sock: socket.socket, buffer: bytes, timeout: float) -> tuple[dict | None, bytes]:
    sock.settimeout(timeout)
    while True:
        newline_index = buffer.find(b"\n")
        if newline_index >= 0:
            line = buffer[:newline_index]
            remainder = buffer[newline_index + 1 :]
            if not line:
                buffer = remainder
                continue
            return json.loads(line.decode("utf-8")), remainder
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return None, buffer
        if not chunk:
            raise ConnectionError("socket closed")
        buffer += chunk


def active_joints(no_gripper: bool) -> list[str]:
    joints = list(ARM_JOINTS)
    if not no_gripper:
        joints.append("gripper")
    return joints


def motor_norm_mode(joint_name: str) -> MotorNormMode:
    if joint_name == "gripper":
        return MotorNormMode.RANGE_0_100
    return MotorNormMode.DEGREES


def make_hybrid_unnormalize(original_method, raw_step_joints: set[str], joint_by_id: dict[int, str]):
    def hybrid_unnormalize(self, ids_values: dict[int, float]) -> dict[int, int]:
        result: dict[int, int] = {}
        for motor_id, value in ids_values.items():
            joint_name = joint_by_id[motor_id]
            if joint_name in raw_step_joints:
                result[motor_id] = int(value)
            else:
                result.update(original_method({motor_id: value}))
        return result

    return hybrid_unnormalize


def parse_scales(raw: str, joints: list[str]) -> dict[str, float]:
    if not raw.strip():
        return {joint: 1.0 for joint in joints}
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) != len(joints):
        raise ValueError(f"--scales expects {len(joints)} values for joints {joints}")
    return {joint: float(value) for joint, value in zip(joints, parts)}


def setup_follower_bus(
    *,
    port: str,
    calibration_path: Path,
    joints: list[str],
) -> FeetechMotorsBus:
    calibration = load_calibration(calibration_path)
    missing = [joint for joint in joints if joint not in calibration]
    if missing:
        raise KeyError(f"Follower calibration missing joints: {missing}")

    subset_calibration = {joint: calibration[joint] for joint in joints}
    raw_step_joints = {joint for joint in joints if joint in MULTI_TURN_JOINTS}
    for joint in raw_step_joints:
        subset_calibration[joint].range_min = -MULTI_TURN_RAW_RANGE
        subset_calibration[joint].range_max = MULTI_TURN_RAW_RANGE

    bus = FeetechMotorsBus(
        port=port,
        motors={
            joint: Motor(JOINT_IDS[joint], "sts3215", motor_norm_mode(joint))
            for joint in joints
        },
        calibration=subset_calibration,
    )

    joint_by_id = {JOINT_IDS[joint]: joint for joint in joints}
    bus._unnormalize = types.MethodType(
        make_hybrid_unnormalize(bus._unnormalize, raw_step_joints, joint_by_id),
        bus,
    )

    bus.connect()
    with bus.torque_disabled():
        bus.configure_motors()
        for joint in joints:
            if joint in raw_step_joints:
                bus.write("Lock", joint, 0)
                time.sleep(0.05)
                bus.write("Min_Position_Limit", joint, 0)
                bus.write("Max_Position_Limit", joint, 0)
                bus.write("Operating_Mode", joint, 3)
                time.sleep(0.05)
                bus.write("Lock", joint, 1)
            else:
                bus.write("Operating_Mode", joint, OperatingMode.POSITION.value)
                bus.write("P_Coefficient", joint, 32)
                bus.write("I_Coefficient", joint, 0)
                bus.write("D_Coefficient", joint, 16)
    bus.enable_torque()
    return bus


def run(args: argparse.Namespace) -> None:
    joints = active_joints(args.no_gripper)
    scales = parse_scales(args.scales, joints)
    follower_bus = setup_follower_bus(
        port=args.follower_port,
        calibration_path=args.follower_calibration,
        joints=joints,
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    send_socket_opts(listener)
    listener.bind((args.host, args.port))
    listener.listen(1)
    listener.settimeout(1.0)

    print(f"[follower] listening on {args.host}:{args.port}")
    print(f"[follower] follower port: {args.follower_port}")
    print(f"[follower] follower calibration: {args.follower_calibration}")
    print(f"[follower] joints: {', '.join(joints)}")
    print(f"[follower] scales: {scales}")

    try:
        while True:
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue

            send_socket_opts(conn)
            print(f"[follower] connected by {addr}")
            buffer = b""
            single_turn_start = {
                joint: float(follower_bus.read("Present_Position", joint))
                for joint in joints
                if joint not in MULTI_TURN_JOINTS
            }
            multi_turn_applied = {
                joint: 0.0
                for joint in joints
                if joint in MULTI_TURN_JOINTS
            }

            try:
                while True:
                    message, buffer = recv_json_line(conn, buffer, timeout=1.0)
                    if message is None:
                        continue
                    if message.get("type") != "cmd":
                        continue

                    leader_q = message.get("q", {})
                    command: dict[str, float] = {}
                    for joint in joints:
                        if joint not in leader_q:
                            continue

                        scaled_target = float(leader_q[joint]) * scales[joint]
                        if joint in MULTI_TURN_JOINTS:
                            delta = scaled_target - multi_turn_applied[joint]
                            raw_step = int(round(delta * 4096.0 / 360.0))
                            if raw_step != 0:
                                command[joint] = raw_step
                                multi_turn_applied[joint] += raw_step * 360.0 / 4096.0
                        else:
                            command[joint] = single_turn_start[joint] + scaled_target

                    if command:
                        follower_bus.sync_write("Goal_Position", command)
            except ConnectionError:
                print("[follower] client disconnected")
            finally:
                conn.close()
    finally:
        try:
            follower_bus.disable_torque()
        except Exception:
            pass
        try:
            follower_bus.disconnect()
        except Exception:
            pass
        listener.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal soarm101 teleop follower")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=6666, help="Bind TCP port")
    parser.add_argument("--follower-port", default="/dev/ttyACM1", help="Follower serial port")
    parser.add_argument(
        "--follower-calibration",
        type=Path,
        default=DEFAULT_FOLLOWER_CALIBRATION,
        help="Follower calibration JSON path",
    )
    parser.add_argument(
        "--scales",
        default="",
        help="Per-joint scales in active joint order; use -1 to flip direction",
    )
    parser.add_argument("--no-gripper", action="store_true", help="Disable gripper and run arm-only teleop")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        run(arguments)
    except KeyboardInterrupt:
        print("\n[follower] stopped")
