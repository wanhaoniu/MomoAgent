# SoarmMoce SDK API Reference

Package:
- PyPI: `soarmmoce-sdk`
- Import: `soarmmoce_sdk`

Main entry:
- `from soarmmoce_sdk import Robot`

## Quick Start

```python
from soarmmoce_sdk import Robot

robot = Robot()  # load built-in default config
robot.connect()
state = robot.get_state()
print(state.connected, state.joint_state.q, state.tcp_pose.xyz)
robot.disconnect()
```

## Data Models

`JointState`
- `q: np.ndarray`
- `dq: Optional[np.ndarray]`
- `tau: Optional[np.ndarray]`

`Pose`
- `xyz: np.ndarray` (meters)
- `rpy: np.ndarray` (radians)

`GripperState`
- `available: bool`
- `open_ratio: Optional[float]` (`0.0` closed, `1.0` open)
- `moving: Optional[bool]`

`PermissionState`
- `allow_motion: bool`
- `allow_gripper: bool`
- `allow_home: bool`
- `allow_stop: bool`

`RobotState`
- `connected: bool`
- `joint_state: JointState`
- `tcp_pose: Pose`
- `gripper_state: Optional[GripperState]`
- `permissions: Optional[PermissionState]`
- `timestamp: Optional[float]` (unix seconds)

## Error Types

- `SoarmMoceError` (base)
- `ConnectionError`
- `ProtocolError`
- `TimeoutError`
- `IKError`
- `LimitError`
- `CapabilityError`
- `PermissionError`

## Robot API

### Construction

```python
Robot(
    config_path: Optional[str] = None,
    transport: Optional[TransportBase] = None,
    urdf_path: Optional[str] = None,
    base_link: Optional[str] = None,
    end_link: Optional[str] = None,
)
```

Classmethod:

```python
Robot.from_config(path: str, ...)
```

Properties:
- `connected -> bool`
- `permissions -> PermissionState`

Permission mutation:

```python
robot.set_permissions(
    allow_motion: Optional[bool] = None,
    allow_gripper: Optional[bool] = None,
    allow_home: Optional[bool] = None,
    allow_stop: Optional[bool] = None,
) -> PermissionState
```

### Connection

- `connect() -> None`
- `disconnect() -> None`

### State

- `get_joint_state() -> JointState`
- `get_end_effector_pose(q: Optional[Sequence[float]] = None) -> Pose`
- `get_gripper_state() -> GripperState`
- `get_state() -> RobotState`

### Motion

```python
move_joints(
    q: Sequence[float],
    duration: float = 2.0,
    wait: bool = True,
    timeout: Optional[float] = None,
    speed: Optional[float] = None,
    accel: Optional[float] = None,
) -> None
```

```python
move_pose(
    xyz: Sequence[float],
    rpy: Sequence[float],
    q0: Optional[Sequence[float]] = None,
    seed_policy: str = "current",
    duration: float = 2.0,
    wait: bool = True,
    timeout: Optional[float] = None,
    speed: Optional[float] = None,
    accel: Optional[float] = None,
) -> np.ndarray
```

```python
move_tcp(
    x: float,
    y: float,
    z: float,
    rpy: Optional[Sequence[float]] = None,
    frame: str = "base",  # "base" absolute target, "tool" local offset
    duration: float = 2.0,
    wait: bool = True,
    timeout: Optional[float] = None,
) -> np.ndarray
```

```python
rotate_joint(
    joint: Union[int, str],
    delta_deg: Optional[float] = None,
    target_deg: Optional[float] = None,  # exactly one of delta_deg/target_deg
    duration: float = 1.0,
    wait: bool = True,
    timeout: Optional[float] = None,
    speed: Optional[float] = None,
    accel: Optional[float] = None,
) -> np.ndarray
```

### High-Level

- `home(duration: float = 2.0, wait: bool = True, timeout: Optional[float] = None) -> np.ndarray`
- `set_gripper(open_ratio: float, wait: bool = True, timeout: Optional[float] = None) -> None`
- `stop() -> None`
- `wait_until_stopped(timeout: Optional[float] = None) -> None`

## Wait/Timeout Semantics

- `wait=True` means the API calls `wait_until_stopped`.
- Some transports cannot confirm physical completion; they use best-effort behavior.
- `TimeoutError` is raised if wait times out.

## Configuration Keys (default config)

Core:
- `transport.type`: `mock` | `tcp` | `serial`
- `urdf.path`: supports `pkg://...`
- `protocol.*`: tcp protocol options

Permissions:
- `permissions.allow_motion`
- `permissions.allow_gripper`
- `permissions.allow_home`
- `permissions.allow_stop`

Example:

```yaml
transport:
  type: mock

permissions:
  allow_motion: true
  allow_gripper: true
  allow_home: true
  allow_stop: true
```
