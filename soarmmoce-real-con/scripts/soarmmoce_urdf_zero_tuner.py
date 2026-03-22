#!/usr/bin/env python3
"""Visual tuner for baking soarmMoce joint zero offsets into the URDF."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict

import meshcat
import numpy as np
from meshcat import geometry as mg
from scipy.spatial.transform import Rotation as R

if not hasattr(np, "float"):
    np.float = float  # pragma: no cover - urdfpy still references this alias

from urdfpy import URDF

from soarmmoce_sdk import DEFAULT_MODEL_OFFSETS_DEG


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URDF_PATH = SKILL_ROOT / "resources" / "urdf" / "soarmoce_urdf.urdf"
SDK_TO_URDF_JOINT = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}
URDF_TO_SDK_JOINT = {value: key for key, value in SDK_TO_URDF_JOINT.items()}
DEFAULT_OFFSETS_DEG = {
    name: float(DEFAULT_MODEL_OFFSETS_DEG.get(name, 0.0))
    for name in SDK_TO_URDF_JOINT
}


@contextlib.contextmanager
def _pushd(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _format_floats(values: list[float]) -> str:
    return " ".join(f"{float(v):.6f}" for v in values)


def _parse_xyz(raw: str | None) -> np.ndarray:
    text = str(raw or "0 0 0").strip()
    parts = [float(item) for item in text.split()]
    if len(parts) != 3:
        raise ValueError(f"Expected 3 values, got {text!r}")
    return np.asarray(parts, dtype=float)


def _parse_offsets_json(raw: str | None) -> Dict[str, float]:
    offsets = dict(DEFAULT_OFFSETS_DEG)
    if not raw:
        return offsets
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("--offsets-json must be a JSON object")
    for joint_name, value in payload.items():
        key = str(joint_name).strip()
        sdk_joint = URDF_TO_SDK_JOINT.get(key, key)
        if sdk_joint not in SDK_TO_URDF_JOINT:
            raise ValueError(f"Unknown joint in --offsets-json: {joint_name!r}")
        offsets[sdk_joint] = float(value)
    return offsets


def _load_urdf(urdf_path: Path) -> URDF:
    urdf_path = urdf_path.resolve()
    with _pushd(urdf_path.parent):
        return URDF.load(urdf_path.name)


def _patched_joint_origins(urdf_path: Path, offsets_deg: Dict[str, float]) -> tuple[ET.ElementTree, Dict[str, Dict[str, object]]]:
    tree = ET.parse(urdf_path)
    root = tree.getroot()
    diagnostics: Dict[str, Dict[str, object]] = {}

    for sdk_joint, offset_deg in offsets_deg.items():
        urdf_joint = SDK_TO_URDF_JOINT[sdk_joint]
        if abs(float(offset_deg)) < 1e-9:
            continue

        joint_elem = root.find(f"./joint[@name='{urdf_joint}']")
        if joint_elem is None:
            raise KeyError(f"Joint {urdf_joint!r} not found in {urdf_path}")

        origin_elem = joint_elem.find("origin")
        if origin_elem is None:
            origin_elem = ET.SubElement(joint_elem, "origin")
            origin_elem.set("xyz", "0 0 0")
            origin_elem.set("rpy", "0 0 0")

        axis_elem = joint_elem.find("axis")
        axis_xyz = _parse_xyz(axis_elem.get("xyz") if axis_elem is not None else "0 0 1")
        axis_norm = float(np.linalg.norm(axis_xyz))
        if axis_norm < 1e-9:
            raise ValueError(f"Joint {urdf_joint!r} has invalid axis {axis_xyz}")
        axis_unit = axis_xyz / axis_norm

        current_xyz = _parse_xyz(origin_elem.get("xyz"))
        current_rpy = _parse_xyz(origin_elem.get("rpy"))
        current_rot = R.from_euler("xyz", current_rpy)
        delta_rot = R.from_rotvec(axis_unit * np.deg2rad(float(offset_deg)))
        new_rot = current_rot * delta_rot
        new_rpy = new_rot.as_euler("xyz", degrees=False)

        origin_elem.set("xyz", _format_floats(list(current_xyz)))
        origin_elem.set("rpy", _format_floats(list(new_rpy)))

        diagnostics[sdk_joint] = {
            "urdf_joint": urdf_joint,
            "offset_deg": float(offset_deg),
            "axis_xyz": [float(v) for v in axis_unit],
            "old_rpy_rad": [float(v) for v in current_rpy],
            "new_rpy_rad": [float(v) for v in new_rpy],
            "new_rpy_deg": [float(v) for v in np.rad2deg(new_rpy)],
        }

    return tree, diagnostics


def _rewrite_mesh_paths(tree: ET.ElementTree, source_urdf_path: Path, output_urdf_path: Path) -> None:
    for mesh_elem in tree.getroot().iterfind(".//mesh"):
        filename = str(mesh_elem.get("filename") or "").strip()
        if not filename:
            continue
        source_mesh_path = (source_urdf_path.parent / filename).resolve()
        rel_path = os.path.relpath(source_mesh_path, output_urdf_path.parent)
        mesh_elem.set("filename", str(rel_path))


def _write_temp_patched_urdf(urdf_path: Path, offsets_deg: Dict[str, float]) -> tuple[Path, Dict[str, Dict[str, object]]]:
    tree, diagnostics = _patched_joint_origins(urdf_path, offsets_deg)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".urdf",
        prefix=".soarmmoce_zero_tuner_",
        dir=urdf_path.parent,
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        tree.write(handle, encoding="unicode", xml_declaration=True)
    finally:
        handle.close()
    return tmp_path, diagnostics


def _pose_cfg_from_offsets(offsets_deg: Dict[str, float]) -> Dict[str, float]:
    return {
        SDK_TO_URDF_JOINT[sdk_joint]: float(np.deg2rad(offset_deg))
        for sdk_joint, offset_deg in offsets_deg.items()
        if sdk_joint in SDK_TO_URDF_JOINT
    }


def _material(color: int, opacity: float) -> mg.MeshLambertMaterial:
    return mg.MeshLambertMaterial(
        color=int(color),
        transparent=bool(opacity < 0.999),
        opacity=float(opacity),
        reflectivity=0.2,
    )


def _set_scene_geometry(vis: meshcat.Visualizer, prefix: str, robot: URDF, cfg: Dict[str, float], color: int, opacity: float) -> None:
    vis[prefix].delete()
    fk = robot.visual_trimesh_fk(cfg=cfg)
    material = _material(color, opacity)
    for idx, (mesh, transform) in enumerate(fk.items()):
        node = vis[prefix][str(idx)]
        node.set_object(
            mg.TriangularMeshGeometry(
                vertices=np.asarray(mesh.vertices, dtype=float),
                faces=np.asarray(mesh.faces, dtype=np.int32),
            ),
            material,
        )
        node.set_transform(np.asarray(transform, dtype=float))


def _print_summary(offsets_deg: Dict[str, float], diagnostics: Dict[str, Dict[str, object]]) -> None:
    print("\nCurrent offset hypothesis (deg):")
    print(json.dumps(offsets_deg, ensure_ascii=False, indent=2))
    if diagnostics:
        print("\nBaked joint origin updates:")
        for sdk_joint, info in diagnostics.items():
            print(
                f"- {sdk_joint} -> {info['urdf_joint']}: "
                f"offset={info['offset_deg']:.3f} deg, "
                f"new_rpy_rad={info['new_rpy_rad']}"
            )


def _render_scene(vis: meshcat.Visualizer, urdf_path: Path, offsets_deg: Dict[str, float]) -> tuple[Path, Dict[str, Dict[str, object]]]:
    raw_robot = _load_urdf(urdf_path)
    patched_path, diagnostics = _write_temp_patched_urdf(urdf_path, offsets_deg)
    patched_robot = _load_urdf(patched_path)
    offset_pose_cfg = _pose_cfg_from_offsets(offsets_deg)

    _set_scene_geometry(vis, "raw_zero", raw_robot, cfg={}, color=0x8E8E8E, opacity=0.22)
    _set_scene_geometry(vis, "raw_plus_offset", raw_robot, cfg=offset_pose_cfg, color=0x2F6FDB, opacity=0.25)
    _set_scene_geometry(vis, "baked_zero", patched_robot, cfg={}, color=0xF38A2E, opacity=0.70)

    print("\nMeshcat layers:")
    print("- gray: original URDF at q=0")
    print("- blue: original URDF with the current offset hypothesis applied as joint angles")
    print("- orange: patched URDF at q=0")
    _print_summary(offsets_deg, diagnostics)
    return patched_path, diagnostics


def _write_output_urdf(source_path: Path, output_path: Path, offsets_deg: Dict[str, float]) -> Dict[str, Dict[str, object]]:
    tree, diagnostics = _patched_joint_origins(source_path, offsets_deg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_mesh_paths(tree, source_path.resolve(), output_path.resolve())
    tree.write(output_path, encoding="unicode", xml_declaration=True)
    return diagnostics


def _interactive_loop(vis: meshcat.Visualizer, urdf_path: Path, offsets_deg: Dict[str, float]) -> None:
    temp_files: list[Path] = []
    patched_path, _ = _render_scene(vis, urdf_path, offsets_deg)
    temp_files.append(patched_path)

    help_text = (
        "\nCommands:\n"
        "  set <joint> <deg>   Set offset in degrees. Joint can use SDK or URDF name.\n"
        "  add <joint> <deg>   Increment offset in degrees.\n"
        "  show                Print current offsets.\n"
        "  reset               Reset all offsets to 0.\n"
        "  write <path>        Write a patched URDF file.\n"
        "  quit                Exit.\n"
    )
    print(help_text)

    try:
        while True:
            raw = input("zero-tuner> ").strip()
            if not raw:
                continue
            parts = raw.split()
            cmd = parts[0].lower()

            if cmd in {"q", "quit", "exit"}:
                break
            if cmd == "help":
                print(help_text)
                continue
            if cmd == "show":
                _print_summary(offsets_deg, {})
                continue
            if cmd == "reset":
                for key in offsets_deg:
                    offsets_deg[key] = 0.0
            elif cmd in {"set", "add"}:
                if len(parts) != 3:
                    print("Expected: set <joint> <deg> or add <joint> <deg>")
                    continue
                raw_joint = parts[1].strip()
                sdk_joint = URDF_TO_SDK_JOINT.get(raw_joint, raw_joint)
                if sdk_joint not in offsets_deg:
                    print(f"Unknown joint: {raw_joint}")
                    continue
                delta = float(parts[2])
                if cmd == "set":
                    offsets_deg[sdk_joint] = delta
                else:
                    offsets_deg[sdk_joint] += delta
            elif cmd == "write":
                if len(parts) != 2:
                    print("Expected: write <output.urdf>")
                    continue
                output_path = Path(parts[1]).expanduser().resolve()
                diagnostics = _write_output_urdf(urdf_path, output_path, offsets_deg)
                print(f"Wrote patched URDF to {output_path}")
                _print_summary(offsets_deg, diagnostics)
                continue
            else:
                print(f"Unknown command: {cmd}. Type 'help'.")
                continue

            patched_path, _ = _render_scene(vis, urdf_path, offsets_deg)
            temp_files.append(patched_path)
    finally:
        for path in temp_files:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Visual tuner for baking soarmMoce joint zero offsets into the URDF")
    parser.add_argument("--urdf", default=str(DEFAULT_URDF_PATH), help="Path to the source URDF")
    parser.add_argument(
        "--offsets-json",
        default=json.dumps(DEFAULT_OFFSETS_DEG),
        help='Initial SDK-style offsets in degrees, e.g. {"shoulder_lift": -90, "wrist_flex": -180}',
    )
    parser.add_argument(
        "--write-output",
        default="",
        help="Write one patched URDF and exit instead of entering the interactive loop",
    )
    args = parser.parse_args()

    urdf_path = Path(args.urdf).expanduser().resolve()
    offsets_deg = _parse_offsets_json(args.offsets_json)

    if args.write_output:
        output_path = Path(args.write_output).expanduser().resolve()
        diagnostics = _write_output_urdf(urdf_path, output_path, offsets_deg)
        print(f"Wrote patched URDF to {output_path}")
        _print_summary(offsets_deg, diagnostics)
        return

    vis = meshcat.Visualizer()
    print(f"Meshcat URL: {vis.url()}")
    _interactive_loop(vis, urdf_path, offsets_deg)


if __name__ == "__main__":
    main()
