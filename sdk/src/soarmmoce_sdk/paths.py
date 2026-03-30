from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
SDK_RESOURCE_ROOT = PACKAGE_ROOT / "resources"
REPO_ROOT = PACKAGE_ROOT.parents[2]
REPO_SKILL_ROOT = REPO_ROOT / "skills" / "soarmmoce-real-con"
OPENCLAW_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "soarmmoce-real-con"


def candidate_skill_roots() -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in (REPO_SKILL_ROOT, OPENCLAW_SKILL_ROOT):
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def preferred_skill_root() -> Path:
    for path in candidate_skill_roots():
        if path.exists():
            return path
    return REPO_SKILL_ROOT


def skill_resource_root() -> Path:
    return preferred_skill_root() / "resources"


def skill_runtime_dir() -> Path:
    for root in candidate_skill_roots():
        runtime_dir = root / "workspace" / "runtime"
        if runtime_dir.exists():
            return runtime_dir
    return preferred_skill_root() / "workspace" / "runtime"


def skill_picture_dir() -> Path:
    return preferred_skill_root() / "workspace" / "picture"


def skill_calibration_dir() -> Path:
    for root in candidate_skill_roots():
        calib_dir = root / "calibration"
        if calib_dir.exists():
            return calib_dir
    return preferred_skill_root() / "calibration"
