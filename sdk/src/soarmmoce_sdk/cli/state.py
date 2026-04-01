from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path
from typing import Any, Dict

import kinpy as kp
import numpy as np

from ..cli_common import run_and_print
from ..paths import skill_resource_root
from ..real_arm import DEFAULT_MODEL_OFFSETS_DEG, SoArmMoceController


REFERENCE_SO101_URDF_PATH = skill_resource_root() / "references" / "so101.urdf"
DEFAULT_OFFSETS_DEG = dict(DEFAULT_MODEL_OFFSETS_DEG)


def _parse_offsets_json(raw: str) -> Dict[str, float]:
    if not raw:
        return dict(DEFAULT_OFFSETS_DEG)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("--offsets-json must be a JSON object")
    return {str(k): float(v) for k, v in payload.items()}


def _build_chain(urdf_path: Path, end_link_name: str):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return kp.build_serial_chain_from_urdf(urdf_path.read_text(encoding="utf-8").encode("utf-8"), end_link_name)


def _pose_dict(tf: kp.Transform) -> Dict[str, Any]:
    return {
        "xyz": np.asarray(tf.pos, dtype=float),
        "rpy": np.asarray(tf.rot_euler, dtype=float),
        "quat_wxyz": np.asarray(tf.rot, dtype=float),
    }


def _joint_probe_report(chain, q_deg: np.ndarray, joint_names: list[str]) -> Dict[str, Dict[str, float]]:
    base_tf = chain.forward_kinematics(np.deg2rad(q_deg))
    base_xyz = np.asarray(base_tf.pos, dtype=float)
    report: Dict[str, Dict[str, float]] = {}
    for idx, joint_name in enumerate(joint_names):
        q_probe = q_deg.copy()
        q_probe[idx] += 1.0
        probe_tf = chain.forward_kinematics(np.deg2rad(q_probe))
        delta = np.asarray(probe_tf.pos, dtype=float) - base_xyz
        report[joint_name] = {
            "dx_per_plus1deg": float(delta[0]),
            "dy_per_plus1deg": float(delta[1]),
            "dz_per_plus1deg": float(delta[2]),
        }
    return report


def _chain_report(chain, q_deg: np.ndarray, joint_names: list[str]) -> Dict[str, Any]:
    tf = chain.forward_kinematics(np.deg2rad(q_deg))
    return {
        "joint_names": list(chain.get_joint_parameter_names()),
        "tcp_pose": _pose_dict(tf),
        "probe_plus1deg": _joint_probe_report(chain, q_deg, joint_names),
    }


def diagnose_model(offsets_deg: Dict[str, float]) -> Dict[str, Any]:
    arm = SoArmMoceController()
    state_payload = arm.read()
    state = state_payload["state"]
    q_deg = np.array([float(state["joint_state"][name]) for name in arm.meta()["joint_names"]], dtype=float)
    bus = arm._ensure_bus()
    raw_motor = bus.sync_read("Present_Position", normalize=False)

    active_chain = arm._ensure_kin_chain()
    active_joint_names = list(active_chain.get_joint_parameter_names())
    active_offsets_vec = np.array(
        [float(arm.config.model_offsets_deg.get(name, 0.0)) for name in active_joint_names],
        dtype=float,
    )
    active_report = _chain_report(active_chain, q_deg + active_offsets_vec, active_joint_names)

    offsets_vec = np.array([float(offsets_deg.get(name, 0.0)) for name in active_joint_names], dtype=float)
    active_offset_report = _chain_report(active_chain, q_deg + offsets_vec, active_joint_names)

    so101_path = REFERENCE_SO101_URDF_PATH
    so101_report = None
    if so101_path.exists():
        so101_chain = _build_chain(so101_path, "gripper_frame_link")
        so101_joint_names = list(so101_chain.get_joint_parameter_names())
        if len(so101_joint_names) >= q_deg.shape[0]:
            q_so101 = q_deg.copy()
            so101_report = _chain_report(so101_chain, q_so101, so101_joint_names[: q_deg.shape[0]])

    calib_path = arm.config.calib_dir / f"{arm.config.robot_id}.json"
    calibration = json.loads(calib_path.read_text(encoding="utf-8")) if calib_path.exists() else None
    return {
        "note": "read-only model diagnosis, does not move hardware",
        "active_config": {
            "urdf_path": str(arm.config.urdf_path),
            "target_frame": str(arm.config.target_frame),
            "calibration_path": str(calib_path),
            "model_offsets_deg": dict(arm.config.model_offsets_deg),
        },
        "raw_motor_present_position": raw_motor,
        "joint_state_deg": state["joint_state"],
        "calibration": calibration,
        "models": {
            "active_urdf": active_report,
            "active_urdf_with_offset_hypothesis": {
                "offsets_deg": offsets_deg,
                **active_offset_report,
            },
            "so101_urdf_reference": so101_report,
        },
    }


def multi_turn_state_payload(arm: SoArmMoceController | None = None) -> Dict[str, Any]:
    owned_arm = arm is None
    arm = arm or SoArmMoceController()
    try:
        return arm.get_multi_turn_debug_state()
    finally:
        if owned_arm:
            arm.close()


def multi_turn_angles_payload(arm: SoArmMoceController | None = None) -> Dict[str, Any]:
    owned_arm = arm is None
    arm = arm or SoArmMoceController()
    try:
        state = arm.get_state()
        return {
            name: float(state["joint_state"][name])
            for name in arm.meta()["multi_turn_joints"]
            if name in state["joint_state"]
        }
    finally:
        if owned_arm:
            arm.close()


def _print_json_payload(payload: Dict[str, Any]) -> None:
    print(json.dumps({"ok": True, "result": payload, "error": None}, ensure_ascii=False, indent=2), flush=True)


def watch_payload(interval: float, payload_factory) -> None:
    sleep_s = max(0.05, float(interval))
    arm = SoArmMoceController()
    try:
        while True:
            _print_json_payload(payload_factory(arm))
            time.sleep(sleep_s)
    finally:
        arm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read current soarmMoce state")
    parser.add_argument(
        "--diag-model",
        action="store_true",
        help="Print read-only kinematic diagnosis for current joint state",
    )
    parser.add_argument(
        "--offsets-json",
        default=json.dumps(DEFAULT_OFFSETS_DEG),
        help='JSON object for offset hypothesis, e.g. {"shoulder_lift": -90, "wrist_flex": -180}',
    )
    parser.add_argument(
        "--multi-turn",
        action="store_true",
        help="Print multi-turn runtime state, raw present position, and recent multi-turn goals",
    )
    parser.add_argument(
        "--multi-turn-angles",
        action="store_true",
        help="Print only the current angle values of multi-turn joints",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously print the selected payload until interrupted",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="Watch refresh interval in seconds",
    )
    args = parser.parse_args()

    if args.diag_model:
        if args.watch:
            raise ValueError("--watch is not supported together with --diag-model")
        run_and_print(lambda: diagnose_model(_parse_offsets_json(args.offsets_json)))
        return
    if args.multi_turn:
        if args.watch:
            watch_payload(args.interval, multi_turn_state_payload)
            return
        run_and_print(multi_turn_state_payload)
        return
    if args.multi_turn_angles:
        if args.watch:
            watch_payload(args.interval, multi_turn_angles_payload)
            return
        run_and_print(multi_turn_angles_payload)
        return
    if args.watch:
        raise ValueError("--watch currently requires --multi-turn or --multi-turn-angles")
    run_and_print(lambda: SoArmMoceController().read())


if __name__ == "__main__":
    main()
