# Direct SDK Contract

Use this reference when writing external-agent tools that import `soarmmoce_sdk` directly.

## Imports

```python
from soarmmoce_sdk import JOINTS, SoArmMoceController, resolve_config, to_jsonable
```

Create a controller:

```python
arm = SoArmMoceController(resolve_config(None))
```

Use an explicit config path if the copied repo is not using the default serial config:

```python
arm = SoArmMoceController(resolve_config("sdk/src/soarmmoce_sdk/resources/configs/soarm_moce_serial.yaml"))
```

Always close:

```python
arm.close(disable_torque=False)
```

Use `disable_torque=True` only when the human is holding the arm and explicitly wants free movement.

## State

```python
state = arm.get_state()
```

Important fields:

- `state["joint_state"]["names"]`: joint order.
- `state["joint_state"]["q"]`: joint values in radians.
- `state["joint_state"]["shoulder_pan"]`: named joint value in degrees.
- `state["tcp_pose"]["xyz"]`: TCP position in meters.
- `state["tcp_pose"]["rpy"]`: TCP orientation in radians.
- `state["gripper_state"]["open_ratio"]`: gripper ratio when available.

Joint names:

- `shoulder_pan`
- `shoulder_lift`
- `elbow_flex`
- `wrist_flex`
- `wrist_roll`

Optional gripper name:

- `gripper`

## Joint Motion

Relative joint move:

```python
result = arm.move_joint(
    joint="shoulder_pan",
    delta_deg=3.0,
    duration=1.0,
    speed_percent=30,
    wait=True,
    timeout=4.0,
)
```

Absolute joint target, relative to the SDK's current startup/reference semantics:

```python
result = arm.move_joint(
    joint="wrist_roll",
    target_deg=0.0,
    duration=1.0,
    speed_percent=30,
    wait=True,
    timeout=4.0,
)
```

Multiple joints:

```python
result = arm.move_joints(
    {"shoulder_pan": 0.0, "wrist_flex": 5.0},
    duration=1.5,
    speed_percent=30,
    wait=True,
    timeout=5.0,
)
```

## Cartesian Motion

Small relative move:

```python
result = arm.move_delta(
    dx=0.0,
    dy=0.0,
    dz=0.01,
    frame="base",
    duration=1.0,
    speed_percent=25,
    wait=True,
    timeout=5.0,
)
```

Absolute pose:

```python
result = arm.move_pose(
    xyz=[0.25, 0.0, 0.20],
    rpy=None,
    seed_policy="current",
    duration=2.0,
    speed_percent=25,
    wait=True,
    timeout=8.0,
)
```

Units:

- Position: meters.
- Orientation: radians.
- `frame="base"`: base/world frame.
- `frame="tool"`: current tool frame.

## Gripper

```python
arm.set_gripper(open_ratio=1.0, duration=1.0, speed_percent=40, wait=True, timeout=3.0)
arm.open_gripper(duration=1.0, speed_percent=40, wait=True, timeout=3.0)
arm.close_gripper(duration=1.0, speed_percent=40, wait=True, timeout=3.0)
```

`open_ratio=0.0` means closed. `open_ratio=1.0` means open.

## Home, Stop, Torque

```python
arm.home(duration=1.5, speed_percent=30, wait=True, timeout=5.0)
arm.stop()
arm.enable_torque()
arm.disable_torque()
```

`disable_torque()` can make the arm drop. Use only when intentional.

## Agent Tool Recommendations

Expose these direct tools to the external agent:

- `robot_state`
- `robot_joint_delta`
- `robot_joint_target`
- `robot_cartesian_delta`
- `robot_gripper`
- `robot_home`
- `robot_stop`

Each tool should return JSON with:

- `ok`
- `command`
- `before`
- `result`
- `after`
- `error` when failed

On exceptions, return the exact error message and do not invent success.
