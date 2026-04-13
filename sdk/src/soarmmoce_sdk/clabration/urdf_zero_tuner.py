#!/usr/bin/env python3
"""Visual tuner for baking soarmMoce joint zero offsets into the URDF.

This tool only adjusts the URDF zero pose by rotating joint origins.
It does not calibrate joint limits. Use the dedicated URDF limit calibration
script for min/max travel measurement.
"""

from __future__ import annotations

import argparse
import collections
import collections.abc
import contextlib
import fractions
import json
import math
import os
import socket
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict

import numpy as np


def _apply_legacy_dependency_compat() -> None:
    """Provide Python 3.10 / NumPy 2.x aliases expected by urdfpy's pinned deps."""
    for name in ("Mapping", "MutableMapping", "Sequence", "Set", "MutableSet", "Iterable"):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))

    if not hasattr(fractions, "gcd"):
        fractions.gcd = math.gcd

    numpy_aliases = {
        "bool": bool,
        "complex": complex,
        "float": float,
        "int": int,
        "object": object,
        "str": str,
        "alltrue": np.all,
        "complex_": np.complex128,
        "float_": np.float64,
        "infty": np.inf,
        "sometrue": np.any,
    }
    for name, value in numpy_aliases.items():
        if name not in np.__dict__:
            setattr(np, name, value)


_apply_legacy_dependency_compat()

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SDK_SRC = PACKAGE_ROOT.parent
SDK_ROOT = SDK_SRC.parent
REPO_ROOT = SDK_ROOT.parent
LEGACY_SKILL_ROOT = REPO_ROOT / "skills" / "soarmmoce-real-con"

sdk_src_str = str(SDK_SRC)
if sdk_src_str not in sys.path:
    sys.path.insert(0, sdk_src_str)

from soarmmoce_sdk import DEFAULT_MODEL_OFFSETS_DEG, resolve_config


SDK_URDF_PATH = PACKAGE_ROOT / "resources" / "urdf" / "soarmoce_urdf.urdf"
LEGACY_URDF_PATH = LEGACY_SKILL_ROOT / "resources" / "urdf" / "soarmoce_urdf.urdf"
SDK_TO_URDF_JOINT = {
    "shoulder_pan": "shoulder",
    "shoulder_lift": "shoulder_lift",
    "elbow_flex": "elbow",
    "wrist_flex": "wrist",
    "wrist_roll": "wrist_roll",
}
URDF_TO_SDK_JOINT = {value: key for key, value in SDK_TO_URDF_JOINT.items()}


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
    return LEGACY_URDF_PATH.resolve()


def _resolve_default_offsets_deg() -> Dict[str, float]:
    offsets = {name: float(DEFAULT_MODEL_OFFSETS_DEG.get(name, 0.0)) for name in SDK_TO_URDF_JOINT}
    try:
        cfg = resolve_config()
        for joint_name, value in dict(getattr(cfg, "model_offsets_deg", {})).items():
            if joint_name in offsets:
                offsets[joint_name] = float(value)
    except Exception:
        pass
    return offsets


DEFAULT_URDF_PATH = _resolve_default_urdf_path()
DEFAULT_OFFSETS_DEG = _resolve_default_offsets_deg()
_MESHCAT_MODULE: Any | None = None
_MESHCAT_GEOMETRY: Any | None = None
_URDF_CLASS: Any | None = None


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


def _require_urdf_class():
    global _URDF_CLASS
    if _URDF_CLASS is not None:
        return _URDF_CLASS
    try:
        from urdfpy import URDF as _URDF
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError(
            "Interactive URDF preview could not import 'urdfpy'. "
            "The soarmmoce environment likely has an incompatible urdfpy/networkx/numpy combination."
        ) from exc
    _URDF_CLASS = _URDF
    return _URDF_CLASS


def _load_urdf(urdf_path: Path):
    urdf_path = urdf_path.resolve()
    URDF = _require_urdf_class()
    with _pushd(urdf_path.parent):
        return URDF.load(urdf_path.name)


def _rotation_matrix_x(angle: float) -> np.ndarray:
    c = math.cos(float(angle))
    s = math.sin(float(angle))
    return np.asarray(
        [
            [1.0, 0.0, 0.0],
            [0.0, c, -s],
            [0.0, s, c],
        ],
        dtype=float,
    )


def _rotation_matrix_y(angle: float) -> np.ndarray:
    c = math.cos(float(angle))
    s = math.sin(float(angle))
    return np.asarray(
        [
            [c, 0.0, s],
            [0.0, 1.0, 0.0],
            [-s, 0.0, c],
        ],
        dtype=float,
    )


def _rotation_matrix_z(angle: float) -> np.ndarray:
    c = math.cos(float(angle))
    s = math.sin(float(angle))
    return np.asarray(
        [
            [c, -s, 0.0],
            [s, c, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


def _rotation_matrix_from_rpy(rpy_xyz: np.ndarray) -> np.ndarray:
    roll = float(rpy_xyz[0])
    pitch = float(rpy_xyz[1])
    yaw = float(rpy_xyz[2])
    return _rotation_matrix_z(yaw) @ _rotation_matrix_y(pitch) @ _rotation_matrix_x(roll)


def _rotation_matrix_from_axis_angle(axis_unit: np.ndarray, angle_rad: float) -> np.ndarray:
    x = float(axis_unit[0])
    y = float(axis_unit[1])
    z = float(axis_unit[2])
    c = math.cos(float(angle_rad))
    s = math.sin(float(angle_rad))
    one_minus_c = 1.0 - c
    return np.asarray(
        [
            [c + x * x * one_minus_c, x * y * one_minus_c - z * s, x * z * one_minus_c + y * s],
            [y * x * one_minus_c + z * s, c + y * y * one_minus_c, y * z * one_minus_c - x * s],
            [z * x * one_minus_c - y * s, z * y * one_minus_c + x * s, c + z * z * one_minus_c],
        ],
        dtype=float,
    )


def _rpy_from_rotation_matrix(matrix: np.ndarray) -> np.ndarray:
    value = float(np.clip(-matrix[2, 0], -1.0, 1.0))
    pitch = math.asin(value)
    cos_pitch = math.cos(pitch)
    if abs(cos_pitch) > 1e-9:
        roll = math.atan2(float(matrix[2, 1]), float(matrix[2, 2]))
        yaw = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    else:
        roll = math.atan2(float(-matrix[1, 2]), float(matrix[1, 1]))
        yaw = 0.0
    return np.asarray([roll, pitch, yaw], dtype=float)


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
        current_rot = _rotation_matrix_from_rpy(current_rpy)
        delta_rot = _rotation_matrix_from_axis_angle(axis_unit, float(np.deg2rad(float(offset_deg))))
        new_rot = current_rot @ delta_rot
        new_rpy = _rpy_from_rotation_matrix(new_rot)

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


def _require_meshcat() -> tuple[Any, Any]:
    global _MESHCAT_MODULE, _MESHCAT_GEOMETRY
    if _MESHCAT_MODULE is not None and _MESHCAT_GEOMETRY is not None:
        return _MESHCAT_MODULE, _MESHCAT_GEOMETRY
    try:
        import meshcat as _meshcat
        from meshcat import geometry as _mg
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError(
            "Interactive zero tuning requires the optional 'meshcat' package in the current Python environment."
        ) from exc
    _MESHCAT_MODULE = _meshcat
    _MESHCAT_GEOMETRY = _mg
    return _MESHCAT_MODULE, _MESHCAT_GEOMETRY


def _assert_interactive_preview_socket_support() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        raise RuntimeError(
            "Interactive preview needs to open a local meshcat server on 127.0.0.1, "
            "but this environment does not allow listening sockets. "
            "Run the command in a normal local terminal, or use --write-output to patch a URDF without preview."
        ) from exc
    finally:
        probe.close()


def _material(color: int, opacity: float):
    _, mg = _require_meshcat()
    return mg.MeshLambertMaterial(
        color=int(color),
        transparent=bool(opacity < 0.999),
        opacity=float(opacity),
        reflectivity=0.2,
    )


def _set_scene_geometry(vis, prefix: str, robot, cfg: Dict[str, float], color: int, opacity: float) -> None:
    _, mg = _require_meshcat()
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


def _render_scene(vis, urdf_path: Path, offsets_deg: Dict[str, float]) -> tuple[Path, Dict[str, Dict[str, object]]]:
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


def _interactive_loop(vis, urdf_path: Path, offsets_deg: Dict[str, float]) -> None:
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

    _assert_interactive_preview_socket_support()
    meshcat, _ = _require_meshcat()
    vis = meshcat.Visualizer()
    print(f"Meshcat URL: {vis.url()}")
    _interactive_loop(vis, urdf_path, offsets_deg)


if __name__ == "__main__":
    main()
