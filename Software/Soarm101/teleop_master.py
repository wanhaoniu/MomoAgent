#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
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
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEADER_CALIBRATION = (
    REPO_ROOT / "Software" / "Master" / "calibration" / "teleoperators" / "so101_leader" / "black_arm_leader.json"
)


def load_calibration(path: Path) -> dict[str, MotorCalibration]:
    with path.open("r", encoding="utf-8") as handle, draccus.config_type("json"):
        return draccus.load(dict[str, MotorCalibration], handle)


def make_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((host, port))
    return sock


def send_json(sock: socket.socket, payload: dict) -> None:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def active_joints(no_gripper: bool) -> list[str]:
    joints = list(ARM_JOINTS)
    if not no_gripper:
        joints.append("gripper")
    return joints


def motor_norm_mode(joint_name: str) -> MotorNormMode:
    if joint_name == "gripper":
        return MotorNormMode.RANGE_0_100
    return MotorNormMode.DEGREES


def setup_leader_bus(
    *,
    port: str,
    calibration_path: Path,
    joints: list[str],
) -> FeetechMotorsBus:
    calibration = load_calibration(calibration_path)
    missing = [joint for joint in joints if joint not in calibration]
    if missing:
        raise KeyError(f"Leader calibration missing joints: {missing}")

    bus = FeetechMotorsBus(
        port=port,
        motors={
            joint: Motor(JOINT_IDS[joint], "sts3215", motor_norm_mode(joint))
            for joint in joints
        },
        calibration={joint: calibration[joint] for joint in joints},
    )

    bus.connect()
    with bus.torque_disabled():
        bus.configure_motors()
        for joint in joints:
            bus.write("Operating_Mode", joint, OperatingMode.POSITION.value)
            bus.write("P_Coefficient", joint, 16)
            bus.write("I_Coefficient", joint, 0)
            bus.write("D_Coefficient", joint, 32)
    bus.disable_torque()
    return bus


def run(args: argparse.Namespace) -> None:
    joints = active_joints(args.no_gripper)
    leader_bus = setup_leader_bus(
        port=args.leader_port,
        calibration_path=args.leader_calibration,
        joints=joints,
    )
    sock: socket.socket | None = None

    try:
        print(f"[master] connecting to {args.ip}:{args.port}")
        sock = make_socket(args.ip, args.port)
        print(f"[master] leader port: {args.leader_port}")
        print(f"[master] leader calibration: {args.leader_calibration}")
        print(f"[master] joints: {', '.join(joints)}")

        start_pose = leader_bus.sync_read("Present_Position")
        multi_turn_last = {
            joint: float(start_pose[joint])
            for joint in joints
            if joint in MULTI_TURN_JOINTS
        }
        multi_turn_accum = {joint: 0.0 for joint in multi_turn_last}

        print("[master] zero pose locked, streaming teleop")
        while True:
            loop_started = time.perf_counter()
            current_pose = leader_bus.sync_read("Present_Position")
            payload: dict[str, float] = {}

            for joint in joints:
                current = float(current_pose[joint])
                start = float(start_pose[joint])
                if joint in MULTI_TURN_JOINTS:
                    delta = current - multi_turn_last[joint]
                    if delta < -180.0:
                        delta += 360.0
                    elif delta > 180.0:
                        delta -= 360.0
                    multi_turn_accum[joint] += delta
                    multi_turn_last[joint] = current
                    payload[joint] = multi_turn_accum[joint]
                else:
                    payload[joint] = current - start

            send_json(sock, {"type": "cmd", "q": payload})

            elapsed = time.perf_counter() - loop_started
            sleep_s = (1.0 / args.hz) - elapsed
            if sleep_s > 0.0:
                time.sleep(sleep_s)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        try:
            leader_bus.disconnect()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal soarm101 teleop master")
    parser.add_argument("--ip", default="127.0.0.1", help="Follower host")
    parser.add_argument("--port", type=int, default=6666, help="Follower TCP port")
    parser.add_argument("--leader-port", default="/dev/ttyACM0", help="Leader serial port")
    parser.add_argument(
        "--leader-calibration",
        type=Path,
        default=DEFAULT_LEADER_CALIBRATION,
        help="Leader calibration JSON path",
    )
    parser.add_argument("--hz", type=float, default=100.0, help="Streaming frequency")
    parser.add_argument("--no-gripper", action="store_true", help="Disable gripper and run arm-only teleop")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        run(arguments)
    except KeyboardInterrupt:
        print("\n[master] stopped")
