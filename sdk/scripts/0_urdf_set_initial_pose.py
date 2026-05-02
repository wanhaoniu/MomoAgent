#!/usr/bin/env python3
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from _robot_script_common import print_json
from soarmmoce_sdk.clabration.urdf_zero_tuner import (
    DEFAULT_URDF_PATH,
    SDK_TO_URDF_JOINT,
    URDF_TO_SDK_JOINT,
    _write_output_urdf,
)


def main() -> None:
    # 在这里填写你希望成为 URDF q=0 初始姿态的关节角度。
    # key 可以用 SDK 关节名，例如 shoulder_lift；不要用硬件舵机 ID。
    ZERO_POSE_DEG = {
        "shoulder_pan": 0.0,
        "shoulder_lift": 0.0,
        "elbow_flex": 0.0,
        "wrist_flex": 0.0,
        "wrist_roll": 0.0,
    }

    # 默认写回 SDK 当前 URDF，插件打开默认 URDF 时会看到新初始姿态。
    URDF_PATH = DEFAULT_URDF_PATH

    # BASE_URDF_PATH=None 时会自动使用同目录 soarmoce_urdf.zero_base.urdf。
    # 第一次运行会把当前 URDF 保存为基准；后续都从基准生成，避免重复累加角度。
    BASE_URDF_PATH: str | Path | None = None

    WRITE_IN_PLACE = True
    CREATE_BACKUP = True

    result = bake_urdf_initial_pose(
        zero_pose_deg=ZERO_POSE_DEG,
        urdf_path=URDF_PATH,
        base_urdf_path=BASE_URDF_PATH,
        write_in_place=WRITE_IN_PLACE,
        create_backup=CREATE_BACKUP,
    )
    print_json(result)


def _resolve_base_path(target_urdf_path: Path, base_urdf_path: str | Path | None) -> Path:
    if base_urdf_path is not None and str(base_urdf_path).strip():
        return Path(base_urdf_path).expanduser().resolve()
    return target_urdf_path.with_name(f"{target_urdf_path.stem}.zero_base{target_urdf_path.suffix}")


def _normalize_zero_pose(zero_pose_deg: dict[str, Any]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for joint_name, value in dict(zero_pose_deg).items():
        key = str(joint_name).strip()
        sdk_joint = URDF_TO_SDK_JOINT.get(key, key)
        if sdk_joint not in SDK_TO_URDF_JOINT:
            raise ValueError(f"未知关节名: {joint_name!r}。可用关节: {', '.join(SDK_TO_URDF_JOINT)}")
        normalized[sdk_joint] = float(value)
    return normalized


def bake_urdf_initial_pose(
    *,
    zero_pose_deg: dict[str, Any],
    urdf_path: str | Path,
    base_urdf_path: str | Path | None = None,
    write_in_place: bool = True,
    create_backup: bool = True,
) -> dict[str, Any]:
    target_urdf_path = Path(urdf_path).expanduser().resolve()
    if not target_urdf_path.exists():
        raise FileNotFoundError(f"URDF 不存在: {target_urdf_path}")

    base_path = _resolve_base_path(target_urdf_path, base_urdf_path)
    base_created = False
    if not base_path.exists():
        base_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_urdf_path, base_path)
        base_created = True

    output_path = target_urdf_path if write_in_place else target_urdf_path.with_name(
        f"{target_urdf_path.stem}.initial_pose{target_urdf_path.suffix}"
    )

    backup_path: Path | None = None
    if write_in_place and create_backup and output_path.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = output_path.with_name(f"{output_path.name}.bak_{stamp}")
        shutil.copy2(output_path, backup_path)

    normalized_pose = _normalize_zero_pose(zero_pose_deg)
    diagnostics = _write_output_urdf(base_path, output_path, normalized_pose)

    return {
        "action": "bake_urdf_initial_pose",
        "note_cn": "填写角度已从稳定基准 URDF 烘焙为目标 URDF 的 q=0 初始姿态。",
        "write_in_place": bool(write_in_place),
        "source_urdf_path": str(target_urdf_path),
        "base_urdf_path": str(base_path),
        "base_created": bool(base_created),
        "output_urdf_path": str(output_path),
        "backup_path": str(backup_path) if backup_path is not None else None,
        "zero_pose_deg": normalized_pose,
        "diagnostics": diagnostics,
    }


if __name__ == "__main__":
    main()
