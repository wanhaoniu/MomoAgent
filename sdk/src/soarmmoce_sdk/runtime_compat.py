from __future__ import annotations
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .real_arm import AttrDict, JOINTS, SoArmMoceController, resolve_config


class SerialRuntimeTransport:
    """Marker transport so older GUI code can still display a transport name."""


class CompatibleRuntimeRobot:
    """Compatibility wrapper for older GUI/API code built around the old runtime."""

    def __init__(self, config_path: str | None = None) -> None:
        self._config_path = str(config_path or "").strip()
        self._resolved_config = resolve_config(self._config_path or None)
        self._controller = SoArmMoceController(self._resolved_config)
        self._transport = SerialRuntimeTransport()
        self.transport_name = type(self._transport).__name__
        self.config = self._load_legacy_config_dict(self._config_path)
        if not isinstance(self.config.get("robot"), dict):
            self.config["robot"] = {}

    @classmethod
    def from_config(cls, config_path: str | None) -> CompatibleRuntimeRobot:
        return cls(config_path)

    @property
    def connected(self) -> bool:
        return self._controller._bus is not None

    @property
    def robot_model(self) -> AttrDict:
        return self._controller.robot_model

    def connect(self) -> None:
        self._controller._ensure_bus()

    def disconnect(self, disable_torque: bool = True) -> None:
        self._controller.close(disable_torque=bool(disable_torque))

    def close(self, disable_torque: bool = True) -> None:
        self.disconnect(disable_torque=disable_torque)

    def get_state(self) -> AttrDict:
        return self._augment_state(self._controller.get_state())

    def read(self) -> AttrDict:
        return self.get_state()

    def meta(self) -> AttrDict:
        return self._controller.meta()

    def has_gripper(self) -> bool:
        return bool(self._controller.has_gripper())

    def move_joints(
        self,
        targets_deg,
        *,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._controller.move_joints(targets_deg, duration=duration, wait=wait, timeout=timeout)

    def move_joint(
        self,
        *,
        joint: str,
        target_deg: float | None = None,
        delta_deg: float | None = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._controller.move_joint(
            joint=joint,
            target_deg=target_deg,
            delta_deg=delta_deg,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )

    def rotate_joint(
        self,
        *,
        joint: str,
        target_deg: float | None = None,
        delta_deg: float | None = None,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> np.ndarray:
        result = self.move_joint(
            joint=joint,
            target_deg=target_deg,
            delta_deg=delta_deg,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )
        return np.asarray(result["state"]["joint_state"]["q"], dtype=float).reshape(-1)

    def move_delta(
        self,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        drx: float = 0.0,
        dry: float = 0.0,
        drz: float = 0.0,
        frame: str = "base",
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._controller.move_delta(
            dx=dx,
            dy=dy,
            dz=dz,
            drx=drx,
            dry=dry,
            drz=drz,
            frame=frame,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )

    def move_pose(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._controller.move_pose(*args, **kwargs)

    def move_tcp(self, *args: Any, **kwargs: Any) -> np.ndarray:
        result = self._controller.move_tcp(*args, **kwargs)
        return np.asarray(result["state"]["joint_state"]["q"], dtype=float).reshape(-1)

    def home(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self._controller.home(duration=duration, wait=wait, timeout=timeout)

    def stop(self) -> dict[str, Any]:
        return self._controller.stop()

    def set_gripper(
        self,
        *,
        open_ratio: float = 1.0,
        duration: float = 1.0,
        wait: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._controller.set_gripper(
            open_ratio=open_ratio,
            duration=duration,
            wait=wait,
            timeout=timeout,
        )

    def open_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self._controller.open_gripper(duration=duration, wait=wait, timeout=timeout)

    def close_gripper(self, *, duration: float = 1.0, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return self._controller.close_gripper(duration=duration, wait=wait, timeout=timeout)

    def _resolve_home_q(self) -> np.ndarray:
        return np.zeros(len(JOINTS), dtype=float)

    @staticmethod
    def _load_legacy_config_dict(config_path: str) -> dict[str, Any]:
        path_text = str(config_path or "").strip()
        if not path_text:
            return {}
        path = Path(path_text).expanduser().resolve()
        if not path.exists():
            return {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _augment_state(self, state: AttrDict) -> AttrDict:
        payload = AttrDict(dict(state))
        gripper_state = payload.get("gripper_state")
        if isinstance(gripper_state, dict):
            payload["gripper_state"] = AttrDict(dict(gripper_state))
        elif gripper_state is None:
            payload["gripper_state"] = AttrDict({"available": False, "open_ratio": None, "moving": None})

        if "moving" not in payload["gripper_state"]:
            payload["gripper_state"]["moving"] = None

        payload["permissions"] = AttrDict(
            {
                "allow_motion": True,
                "allow_gripper": bool(payload["gripper_state"].get("available", False)),
                "allow_home": True,
                "allow_stop": True,
            }
        )
        payload["connected"] = True
        payload["timestamp"] = float(payload.get("timestamp", time.time()))
        payload["twin"] = None
        return payload


__all__ = ["CompatibleRuntimeRobot", "SerialRuntimeTransport"]
