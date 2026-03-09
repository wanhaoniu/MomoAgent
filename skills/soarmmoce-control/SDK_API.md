# SDK API

## Model

The arm is modeled as a 5-DOF task-space robot.

Controlled task variables:
- `x`
- `y`
- `z`
- `tool_pitch`
- `tool_roll`

Not part of IK:
- `gripper` open/close

TCP definition:
- A fixed point in front of the gripper base
- Configured by `kinematics.tcp_offset`

## Core Types

### `JointState`

- `q: np.ndarray`
- `dq: Optional[np.ndarray]`
- `tau: Optional[np.ndarray]`

### `Pose`

Observed full TCP pose.

- `xyz: np.ndarray`
- `rpy: np.ndarray`

### `ToolPose`

5-DOF task pose used by the solver.

- `xyz: np.ndarray`
- `tool_pitch: float`
- `tool_roll: float`

### `RobotState`

- `connected: bool`
- `joint_state: JointState`
- `tcp_pose: Pose`
- `task_pose: ToolPose`
- `gripper_state: Optional[GripperState]`
- `permissions: Optional[PermissionState]`
- `timestamp: Optional[float]`

## Public API

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

```python
Robot.from_config(path: str, ...) -> Robot
```

If `config_path is None`, the package default config is loaded.

### State

```python
connect() -> None
disconnect() -> None
get_joint_state() -> JointState
get_end_effector_pose(q: Optional[Sequence[float]] = None) -> Pose
get_task_pose(q: Optional[Sequence[float]] = None) -> ToolPose
get_state() -> RobotState
get_gripper_state() -> GripperState
```

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
    tool_pitch: float,
    tool_roll: float,
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
    tool_pitch: Optional[float] = None,
    tool_roll: Optional[float] = None,
    frame: str = "base",
    duration: float = 2.0,
    wait: bool = True,
    timeout: Optional[float] = None,
) -> np.ndarray
```

Semantics:
- `frame="base"`: absolute TCP target
- `frame="tool"`: local TCP offset
- If `tool_pitch/tool_roll` is omitted, current task orientation is preserved

```python
rotate_joint(
    joint: Union[int, str],
    delta_deg: Optional[float] = None,
    target_deg: Optional[float] = None,
    duration: float = 1.0,
    wait: bool = True,
    timeout: Optional[float] = None,
    speed: Optional[float] = None,
    accel: Optional[float] = None,
) -> np.ndarray
```

### High-Level

```python
home(duration: float = 2.0, wait: bool = True, timeout: Optional[float] = None) -> np.ndarray
set_gripper(open_ratio: float, wait: bool = True, timeout: Optional[float] = None) -> None
wait_until_stopped(timeout: Optional[float] = None) -> None
stop() -> None
```

## Errors

SDK methods raise SDK error types from `soarmmoce_sdk`:

- `ConnectionError`
- `TimeoutError`
- `IKError`
- `LimitError`
- `CapabilityError`
- `PermissionError`

## Config Keys

```yaml
transport:
  type: mock

permissions:
  allow_motion: true
  allow_gripper: true
  allow_home: true
  allow_stop: true

kinematics:
  locked_joints:
    gripper: 0.0
  tcp_offset: [0.03, 0.0, 0.0]
  yaw_offset_deg: 0.0
```
