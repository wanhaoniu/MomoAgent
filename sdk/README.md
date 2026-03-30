# soarmmoce-sdk

Python SDK for SO-ARM-Moce.

## Install

```bash
pip install soarmmoce-sdk
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Quickstart

### 1) Use package default config

```python
from soarmmoce_sdk import Robot

robot = Robot()  # auto-loads resources/configs/soarm_moce.yaml
robot.connect()

state = robot.get_state()
print("q:", state.joint_state.q)
print("tcp xyz:", state.tcp_pose.xyz)

robot.home(duration=1.0)
robot.disconnect()
```

### 2) Use custom config file

```python
from soarmmoce_sdk import Robot

robot = Robot.from_config("/abs/path/to/soarm_moce.yaml")
robot.connect()
robot.move_pose([0.1, 0.0, 0.15], [0.0, 0.0, 0.0])
robot.disconnect()
```

## Real Arm Controller

The mature real-hardware controller now lives inside the package as
`soarmmoce_sdk.real_arm`.

```python
from soarmmoce_sdk.real_arm import SoArmMoceController

with SoArmMoceController() as arm:
    print(arm.get_state())
```

The repo skill wrapper at `skills/soarmmoce-real-con/scripts/soarmmoce_sdk.py`
re-exports this module so the skill scripts and package SDK share one implementation.

## Optional Dependencies

```bash
pip install "soarmmoce-sdk[sim]"
pip install "soarmmoce-sdk[viz]"
pip install "soarmmoce-sdk[hardware]"
```

Notes:
- `sim` is currently the primary optional extra.
- `viz` and `hardware` are reserved extension groups for future features.

## Capability Notes

- `Robot.set_gripper(open_ratio, ...)` calls transport gripper support when available.
- If the active transport does not implement gripper control, SDK raises `CapabilityError`.
- `wait/timeout` are best-effort for transports that cannot yet report true motion completion.

## Examples

```bash
python examples/00_quickstart_connect.py
python examples/02_movepose_ik.py
python examples/02_movepose_ik.py --config /abs/path/to/soarm_moce.yaml
```

PyBullet demo:

```bash
pip install "soarmmoce-sdk[sim]"
python examples/03_sim.py
```

## Build & Package Check

```bash
python -m build
python -m zipfile -l dist/*.whl | grep "soarmmoce_sdk/resources/"
tar -tzf dist/*.tar.gz | grep "soarmmoce_sdk/resources/"
```
