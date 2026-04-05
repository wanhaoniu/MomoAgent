#!/usr/bin/env python3
"""Interactive URDF joint-limit calibrator for the rebuilt soarmMoce SDK.

Usage model:

1. Before connecting, place the arm at the physical pose that you want to be
   the URDF zero pose (`q=0`) for the joints you are calibrating.
2. The script connects once and uses that startup pose as the measurement zero.
3. For each joint, you manually move to the safe minimum and maximum positions.
4. The captured lower/upper values are saved and can optionally be written back
   into the URDF `<limit>` fields.

This keeps the logic intentionally simple:
- zero tuning is handled separately by `soarmmoce_urdf_zero_tuner.py`
- limit tuning here is only "measure travel around the chosen zero pose"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config
from soarmmoce_sdk.cli_common import run_and_print


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = Path(__file__).resolve().parents[1]
SDK_URDF_PATH = REPO_ROOT / "sdk" / "src" / "soarmmoce_sdk" / "resources" / "urdf" / "soarmoce_urdf.urdf"
SKILL_URDF_PATH = SKILL_ROOT / "resources" / "urdf" / "soarmoce_urdf.urdf"
DEFAULT_JSON_OUTPUT = SKILL_ROOT / "workspace" / "runtime" / "urdf_limit_calibration.json"

SDK_TO_URDF_JOINT = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}


def _resolve_default_urdf_path() -> Path:
    try:
        cfg = resolve_config()
        candidate = Path(cfg.urdf_path).expanduser().resolve()
        if candidate.exists():
            return candidate
    except Exception:
        pass
    if SDK_URDF_PATH.exists():
        return SDK_URDF_PATH.resolve()
    return SKILL_URDF_PATH.resolve()


def _coerce_path(path_text: str | None, *, default: Path) -> Path:
    text = str(path_text or "").strip()
    if not text:
        return default.resolve()
    return Path(text).expanduser().resolve()


def _parse_joint_names(values: list[str] | None) -> list[str]:
    if not values:
        return list(JOINTS)
    requested = [str(value).strip() for value in values if str(value).strip()]
    invalid = [joint_name for joint_name in requested if joint_name not in SDK_TO_URDF_JOINT]
    if invalid:
        raise ValueError(
            "Unsupported joints for URDF limit calibration: "
            + ", ".join(sorted(invalid))
            + ". Supported joints: "
            + ", ".join(SDK_TO_URDF_JOINT)
        )
    deduped: list[str] = []
    for joint_name in requested:
        if joint_name not in deduped:
            deduped.append(joint_name)
    return deduped


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure safe URDF joint limits around a user-chosen zero pose. "
            "Place the robot at the intended q=0 pose before pressing Enter to connect."
        )
    )
    parser.add_argument(
        "--joints",
        nargs="*",
        default=list(JOINTS),
        help="SDK joint names to calibrate. Default: all five arm joints.",
    )
    parser.add_argument(
        "--urdf",
        default=str(_resolve_default_urdf_path()),
        help="Source URDF to read current limits from.",
    )
    parser.add_argument(
        "--json-output",
        default=str(DEFAULT_JSON_OUTPUT),
        help="Where to save the captured calibration summary JSON.",
    )
    parser.add_argument(
        "--write-output",
        default="",
        help="Optional output URDF path. When provided, the captured limits are written to that file.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Patch the source URDF in place instead of writing a separate output file.",
    )
    return parser.parse_args()


def _rewrite_mesh_paths(tree: ET.ElementTree, source_urdf_path: Path, output_urdf_path: Path) -> None:
    for mesh_elem in tree.getroot().iterfind(".//mesh"):
        filename = str(mesh_elem.get("filename") or "").strip()
        if not filename:
            continue
        source_mesh_path = (source_urdf_path.parent / filename).resolve()
        rel_path = os.path.relpath(source_mesh_path, output_urdf_path.parent)
        mesh_elem.set("filename", str(rel_path))


def _read_existing_urdf_limits(urdf_path: Path) -> dict[str, dict[str, float | None]]:
    tree = ET.parse(urdf_path)
    root = tree.getroot()
    results: dict[str, dict[str, float | None]] = {}
    for sdk_joint, urdf_joint in SDK_TO_URDF_JOINT.items():
        lower = None
        upper = None
        joint_elem = root.find(f"./joint[@name='{urdf_joint}']")
        if joint_elem is not None:
            limit_elem = joint_elem.find("limit")
            if limit_elem is not None:
                lower_text = str(limit_elem.get("lower") or "").strip()
                upper_text = str(limit_elem.get("upper") or "").strip()
                lower = float(lower_text) if lower_text else None
                upper = float(upper_text) if upper_text else None
        results[sdk_joint] = {"lower_rad": lower, "upper_rad": upper}
    return results


def _write_urdf_limits(
    *,
    source_urdf_path: Path,
    output_urdf_path: Path,
    limit_updates_rad: dict[str, tuple[float, float]],
) -> None:
    tree = ET.parse(source_urdf_path)
    root = tree.getroot()
    for sdk_joint, (lower_rad, upper_rad) in limit_updates_rad.items():
        urdf_joint = SDK_TO_URDF_JOINT[sdk_joint]
        joint_elem = root.find(f"./joint[@name='{urdf_joint}']")
        if joint_elem is None:
            raise KeyError(f"Joint {urdf_joint!r} not found in {source_urdf_path}")
        limit_elem = joint_elem.find("limit")
        if limit_elem is None:
            limit_elem = ET.SubElement(joint_elem, "limit")
        limit_elem.set("lower", f"{float(lower_rad):.8f}")
        limit_elem.set("upper", f"{float(upper_rad):.8f}")

    output_urdf_path.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_mesh_paths(tree, source_urdf_path.resolve(), output_urdf_path.resolve())
    tree.write(output_urdf_path, encoding="unicode", xml_declaration=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _capture_joint_snapshot(controller: SoArmMoceController, joint_name: str) -> dict[str, Any]:
    state = controller.get_state()
    joint_deg = float(state["joint_state"][joint_name])
    return {
        "joint_deg": joint_deg,
        "joint_rad": math.radians(joint_deg),
        "relative_raw": int(state["relative_raw_position"][joint_name]),
        "present_raw": int(state["raw_present_position"][joint_name]),
        "startup_raw": int(state["startup_raw_position"][joint_name]),
        "tcp_xyz_m": [float(v) for v in state["tcp_pose"]["xyz"]],
        "tcp_rpy_rad": [float(v) for v in state["tcp_pose"]["rpy"]],
        "timestamp": float(state["timestamp"]),
    }


def _print_snapshot(joint_name: str, label: str, snapshot: dict[str, Any]) -> None:
    print(
        f"[limit-cal] {joint_name} {label}: "
        f"joint_deg={snapshot['joint_deg']:.3f}, "
        f"relative_raw={snapshot['relative_raw']}, "
        f"present_raw={snapshot['present_raw']}, "
        f"startup_raw={snapshot['startup_raw']}"
    )


def _prompt_capture(controller: SoArmMoceController, joint_name: str, label: str) -> dict[str, Any] | None:
    prompt = (
        f"\n[limit-cal] 请手动把 {joint_name} 移到安全的{label}位置。"
        "\n按 Enter 抓取当前值，输入 s 跳过该关节，输入 q 退出。"
    )
    while True:
        user_input = input(prompt + "\n> ").strip().lower()
        if user_input in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if user_input in {"s", "skip"}:
            return None

        snapshot = _capture_joint_snapshot(controller, joint_name)
        _print_snapshot(joint_name, label, snapshot)

        confirm = input("按 Enter 接受，输入 r 重抓，输入 s 跳过，输入 q 退出。\n> ").strip().lower()
        if confirm in {"", "y", "yes", "ok"}:
            return snapshot
        if confirm in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if confirm in {"s", "skip"}:
            return None


def _summarize_joint_capture(
    *,
    joint_name: str,
    existing_limits: dict[str, float | None],
    first_snapshot: dict[str, Any],
    second_snapshot: dict[str, Any],
) -> dict[str, Any]:
    ordered = sorted(
        [("first_capture", dict(first_snapshot)), ("second_capture", dict(second_snapshot))],
        key=lambda item: float(item[1]["joint_deg"]),
    )
    lower_label, lower_capture = ordered[0]
    upper_label, upper_capture = ordered[1]
    lower_deg = float(lower_capture["joint_deg"])
    upper_deg = float(upper_capture["joint_deg"])
    lower_rad = math.radians(lower_deg)
    upper_rad = math.radians(upper_deg)

    return {
        "sdk_joint": joint_name,
        "urdf_joint": SDK_TO_URDF_JOINT[joint_name],
        "existing_urdf_limit_rad": dict(existing_limits),
        "existing_urdf_limit_deg": {
            "lower_deg": None
            if existing_limits.get("lower_rad") is None
            else math.degrees(float(existing_limits["lower_rad"])),
            "upper_deg": None
            if existing_limits.get("upper_rad") is None
            else math.degrees(float(existing_limits["upper_rad"])),
        },
        "measured_limit_deg": {
            "lower_deg": lower_deg,
            "upper_deg": upper_deg,
            "span_deg": float(upper_deg - lower_deg),
            "mid_deg": float((upper_deg + lower_deg) * 0.5),
        },
        "measured_limit_rad": {
            "lower_rad": lower_rad,
            "upper_rad": upper_rad,
            "span_rad": float(upper_rad - lower_rad),
            "mid_rad": float((upper_rad + lower_rad) * 0.5),
        },
        "captures": {
            lower_label: lower_capture,
            upper_label: upper_capture,
        },
        "notes": {
            "reference": "All measured angles are relative to the startup pose captured when this script connected.",
            "interpretation": (
                "If the startup pose was already the intended URDF zero pose, these measured lower/upper values "
                "can be written directly into the URDF joint limits."
            ),
        },
    }


def _run() -> dict[str, Any]:
    args = _parse_args()
    joints = _parse_joint_names(args.joints)
    source_urdf_path = _coerce_path(args.urdf, default=_resolve_default_urdf_path())
    json_output_path = _coerce_path(args.json_output, default=DEFAULT_JSON_OUTPUT)

    if not source_urdf_path.exists():
        raise FileNotFoundError(f"URDF not found: {source_urdf_path}")

    write_output_path: Path | None = None
    if bool(args.in_place):
        write_output_path = source_urdf_path
    elif str(args.write_output or "").strip():
        write_output_path = _coerce_path(args.write_output, default=source_urdf_path)

    existing_limits = _read_existing_urdf_limits(source_urdf_path)

    print(
        "[limit-cal] 使用说明：\n"
        "1. 先把机械臂摆到你希望作为 URDF q=0 的物理姿态。\n"
        "2. 再按 Enter 让脚本连接并记录 startup reference。\n"
        "3. 之后脚本会要求你手动移动到每个关节的安全最小/最大位置。\n"
        "4. 本脚本只标定 limit，不自动推断 zero offset。"
    )
    input("[limit-cal] 准备好后按 Enter 开始连接...\n")

    config = resolve_config()
    controller = SoArmMoceController(config)
    captured: dict[str, Any] = {}
    limit_updates_rad: dict[str, tuple[float, float]] = {}
    try:
        controller._ensure_bus()
        controller.disable_torque()
        zero_state = controller.get_state()
        print(
            "[limit-cal] 已连接，并以当前姿态作为测量参考零点。 "
            f"port={config.port} urdf={source_urdf_path}"
        )
        print(
            "[limit-cal] startup joint summary="
            + json.dumps(
                {joint_name: float(zero_state['joint_state'][joint_name]) for joint_name in joints},
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        for joint_name in joints:
            print(
                f"\n[limit-cal] ===== {joint_name} / {SDK_TO_URDF_JOINT[joint_name]} =====\n"
                f"[limit-cal] 当前 URDF limit(rad)={json.dumps(existing_limits.get(joint_name, {}), ensure_ascii=False)}"
            )
            first_snapshot = _prompt_capture(controller, joint_name, "最小极限")
            if first_snapshot is None:
                print(f"[limit-cal] 跳过 {joint_name}")
                continue
            second_snapshot = _prompt_capture(controller, joint_name, "最大极限")
            if second_snapshot is None:
                print(f"[limit-cal] 跳过 {joint_name}")
                continue

            summary = _summarize_joint_capture(
                joint_name=joint_name,
                existing_limits=dict(existing_limits.get(joint_name, {})),
                first_snapshot=first_snapshot,
                second_snapshot=second_snapshot,
            )
            captured[joint_name] = summary
            limit_updates_rad[joint_name] = (
                float(summary["measured_limit_rad"]["lower_rad"]),
                float(summary["measured_limit_rad"]["upper_rad"]),
            )
            print(
                f"[limit-cal] {joint_name} measured lower/upper(deg)="
                f"{summary['measured_limit_deg']['lower_deg']:.3f} / "
                f"{summary['measured_limit_deg']['upper_deg']:.3f}, "
                f"mid={summary['measured_limit_deg']['mid_deg']:.3f}, "
                f"span={summary['measured_limit_deg']['span_deg']:.3f}"
            )
    finally:
        try:
            controller.disable_torque()
        except Exception:
            pass
        controller.close(disable_torque=True)

    if not captured:
        raise RuntimeError("No joint limits were captured.")

    result = {
        "action": "calibrate_urdf_limits",
        "script": "soarmmoce_urdf_limit_calibrate.py",
        "port": str(config.port),
        "urdf_source_path": str(source_urdf_path),
        "json_output_path": str(json_output_path),
        "reference_pose_rule": (
            "The startup pose at connection time is the measurement zero. "
            "Use the captured limits directly only if that startup pose is the intended URDF zero pose."
        ),
        "captured_at_unix_s": time.time(),
        "joint_results": captured,
    }

    _write_json(json_output_path, result)

    if write_output_path is not None:
        _write_urdf_limits(
            source_urdf_path=source_urdf_path,
            output_urdf_path=write_output_path,
            limit_updates_rad=limit_updates_rad,
        )
        result["written_urdf_path"] = str(write_output_path)

    return result


def main() -> None:
    run_and_print(_run)


if __name__ == "__main__":
    main()
