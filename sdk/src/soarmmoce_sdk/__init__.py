"""SoarmMoce SDK public API."""

from .api import (
    CapabilityError,
    ConnectionError,
    GripperState,
    IKError,
    JointState,
    LimitError,
    PermissionError,
    PermissionState,
    Pose,
    ProtocolError,
    Robot,
    RobotState,
    SoarmMoceError,
    TimeoutError,
    TwinState,
)
from .json_utils import to_jsonable

_REAL_ARM_EXPORTS = {
    "ARM_JOINTS",
    "DEFAULT_MODEL_OFFSETS_DEG",
    "JOINTS",
    "MULTI_TURN_JOINTS",
    "HardwareError",
    "IKError",
    "SoArmMoceConfig",
    "SoArmMoceController",
    "ValidationError",
    "resolve_config",
}


def __getattr__(name: str):
    if name in _REAL_ARM_EXPORTS:
        from . import real_arm

        return getattr(real_arm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _REAL_ARM_EXPORTS)

__all__ = [
    "Robot",
    "Pose",
    "JointState",
    "GripperState",
    "PermissionState",
    "RobotState",
    "TwinState",
    "SoarmMoceError",
    "ConnectionError",
    "ProtocolError",
    "TimeoutError",
    "IKError",
    "LimitError",
    "CapabilityError",
    "PermissionError",
    "to_jsonable",
    "SoArmMoceController",
    "SoArmMoceConfig",
    "ValidationError",
    "HardwareError",
    "JOINTS",
    "ARM_JOINTS",
    "MULTI_TURN_JOINTS",
    "DEFAULT_MODEL_OFFSETS_DEG",
    "resolve_config",
]
