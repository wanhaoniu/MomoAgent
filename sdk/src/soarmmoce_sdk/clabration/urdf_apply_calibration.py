#!/usr/bin/env python3
"""Apply URDF zero offsets and measured joint limits into a target URDF file.

This helper is the "save it into the URDF" step after:

1. deciding on a new zero-pose offset hypothesis
2. recording joint limits with soarmmoce_urdf_limit_calibrate.py while the arm
   is physically placed at that intended zero pose on startup

It can update the configured SDK URDF in place and creates a `.bak` backup by
default before overwriting.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if SDK_SRC.exists():
    sdk_src_str = str(SDK_SRC)
    if sdk_src_str not in sys.path:
        sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk.cli_common import run_and_print

from soarmmoce_urdf_zero_tuner import (
    DEFAULT_OFFSETS_DEG,
    DEFAULT_URDF_PATH,
    _parse_offsets_json,
    _patched_joint_origins,
    _rewrite_mesh_paths,
)


SDK_TO_URDF_JOINT = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}
DEFAULT_LIMIT_JSON = (
    Path(__file__).resolve().parents[1] / "workspace" / "runtime" / "urdf_limit_calibration.json"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply zero-offset and limit calibration results into a URDF file."
    )
    parser.add_argument(
        "--urdf",
        default=str(DEFAULT_URDF_PATH),
        help="Source URDF path. Defaults to the currently configured SDK URDF.",
    )
    parser.add_argument(
        "--offsets-json",
        default=json.dumps(DEFAULT_OFFSETS_DEG),
        help='SDK-style zero offsets in degrees, e.g. {"shoulder_lift": 90, "wrist_flex": 90}',
    )
    parser.add_argument(
        "--limit-json",
        default=str(DEFAULT_LIMIT_JSON),
        help="Joint-limit calibration JSON produced by soarmmoce_urdf_limit_calibrate.py.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Write patched URDF to this path. If omitted together with --in-place, prints a summary only.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the source URDF in place. A .bak file is created first by default.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable the automatic .bak backup when using --in-place.",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Calibration JSON not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _load_limit_updates(limit_json_path: Path) -> tuple[dict[str, tuple[float, float]], dict[str, Any]]:
    payload = _read_json(limit_json_path)
    joint_results = payload.get("joint_results")
    if not isinstance(joint_results, dict):
        raise ValueError(f"{limit_json_path} does not contain a valid 'joint_results' object")

    updates: dict[str, tuple[float, float]] = {}
    for joint_name, entry in joint_results.items():
        if joint_name not in SDK_TO_URDF_JOINT or not isinstance(entry, dict):
            continue
        measured = entry.get("measured_limit_rad")
        if not isinstance(measured, dict):
            continue
        lower = float(measured["lower_rad"])
        upper = float(measured["upper_rad"])
        if upper < lower:
            lower, upper = upper, lower
        updates[joint_name] = (lower, upper)
    if not updates:
        raise ValueError(f"No valid measured_limit_rad entries found in {limit_json_path}")
    return updates, payload


def _apply_limit_updates(tree: ET.ElementTree, limit_updates: dict[str, tuple[float, float]]) -> dict[str, dict[str, float]]:
    root = tree.getroot()
    written: dict[str, dict[str, float]] = {}
    for sdk_joint, (lower_rad, upper_rad) in limit_updates.items():
        urdf_joint = SDK_TO_URDF_JOINT[sdk_joint]
        joint_elem = root.find(f"./joint[@name='{urdf_joint}']")
        if joint_elem is None:
            raise KeyError(f"Joint {urdf_joint!r} not found in URDF")
        limit_elem = joint_elem.find("limit")
        if limit_elem is None:
            limit_elem = ET.SubElement(joint_elem, "limit")
        limit_elem.set("lower", f"{float(lower_rad):.8f}")
        limit_elem.set("upper", f"{float(upper_rad):.8f}")
        written[sdk_joint] = {
            "lower_rad": float(lower_rad),
            "upper_rad": float(upper_rad),
        }
    return written


def _write_tree(tree: ET.ElementTree, *, source_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_mesh_paths(tree, source_path.resolve(), output_path.resolve())
    tree.write(output_path, encoding="unicode", xml_declaration=True)


def _run() -> dict[str, Any]:
    args = _parse_args()
    source_urdf_path = Path(args.urdf).expanduser().resolve()
    if not source_urdf_path.exists():
        raise FileNotFoundError(f"URDF not found: {source_urdf_path}")

    offsets_deg = _parse_offsets_json(args.offsets_json)
    limit_json_path = Path(args.limit_json).expanduser().resolve()
    limit_updates, limit_payload = _load_limit_updates(limit_json_path)

    output_path: Path | None = None
    if bool(args.in_place):
        output_path = source_urdf_path
    elif str(args.output or "").strip():
        output_path = Path(args.output).expanduser().resolve()

    tree, zero_diagnostics = _patched_joint_origins(source_urdf_path, offsets_deg)
    written_limits = _apply_limit_updates(tree, limit_updates)

    backup_path: Path | None = None
    if output_path is not None:
        if output_path == source_urdf_path and not bool(args.no_backup):
            backup_path = source_urdf_path.with_suffix(source_urdf_path.suffix + ".bak")
            shutil.copy2(source_urdf_path, backup_path)
        _write_tree(tree, source_path=source_urdf_path, output_path=output_path)

    joint_mid_summary: dict[str, float] = {}
    joint_results = limit_payload.get("joint_results", {})
    if isinstance(joint_results, dict):
        for joint_name, entry in joint_results.items():
            if joint_name not in SDK_TO_URDF_JOINT or not isinstance(entry, dict):
                continue
            measured_deg = entry.get("measured_limit_deg")
            if isinstance(measured_deg, dict) and "mid_deg" in measured_deg:
                joint_mid_summary[joint_name] = float(measured_deg["mid_deg"])

    return {
        "action": "apply_urdf_calibration",
        "source_urdf_path": str(source_urdf_path),
        "output_urdf_path": str(output_path) if output_path is not None else None,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "offsets_deg": offsets_deg,
        "zero_diagnostics": zero_diagnostics,
        "written_limits_rad": written_limits,
        "limit_json_path": str(limit_json_path),
        "limit_mid_deg_summary": joint_mid_summary,
        "note": (
            "If the limit midpoints stay close to 0 deg after you chose the startup pose, "
            "that is a good sign that the baked zero pose is reasonable."
        ),
    }


def main() -> None:
    run_and_print(_run)


if __name__ == "__main__":
    main()
