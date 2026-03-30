from __future__ import annotations

import importlib.resources as resources
import json
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_PACKAGE_NAME = "soarmmoce_sdk"


def _default_config_path() -> Path:
    res = resources.files(_PACKAGE_NAME) / "resources" / "configs" / "soarm_moce.yaml"
    with resources.as_file(res) as path:
        return Path(path)


def _resolve_pkg_resource_uri(uri: str) -> Path:
    rel = str(uri[len("pkg://") :]).strip()
    if not rel or "/" not in rel:
        raise ValueError(f"Invalid pkg URI: {uri!r}")
    _pkg, rel_path = rel.split("/", 1)
    try:
        res = resources.files(_PACKAGE_NAME) / rel_path
    except ModuleNotFoundError as exc:
        raise FileNotFoundError(f"Package not found for URI {uri!r}") from exc
    with resources.as_file(res) as path:
        return Path(path)


def resolve_path(path: Optional[str], *, default: Optional[Path] = None) -> Path:
    if path is None:
        if default is None:
            raise ValueError("Path is required when no default is provided")
        return Path(default)
    raw = str(path).strip()
    if not raw:
        if default is None:
            raise ValueError("Path is required when no default is provided")
        return Path(default)
    if raw.startswith("pkg://"):
        return _resolve_pkg_resource_uri(raw)
    return Path(raw).expanduser()


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    config_path = resolve_path(path, default=_default_config_path())
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")
    return dict(payload)


def load_calibration_json(path: str) -> Dict[str, Any]:
    payload = json.loads(resolve_path(path).read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"Calibration file must contain a JSON object: {path}")
    return dict(payload)
