---
name: momo-direct-robot-control
description: Integrate an external agent directly with the MomoAgent repository and soarmmoce_sdk for direct robotic arm control. Use when copying this repo into another agent, importing the SDK, writing direct-control tools, choosing required files, running SDK scripts, or documenting safety rules for direct MomoAgent arm operation without an HTTP service layer.
---

# Momo Direct Robot Control

## Architecture

Use the SDK directly when the external agent should own robot control:

```text
external agent -> local tool/function -> soarmmoce_sdk -> serial bus -> hardware
```

The HTTP `momo_robot_service` is optional. It is useful for GUI/Web/App sharing, but it is not required for a single external agent that directly controls the arm.

If deciding what to copy into another agent workspace, read `references/copy-checklist.md`. If writing direct SDK calls or tool schemas, read `references/direct-sdk-contract.md`.

## Install

Preserve the repo layout, then install the SDK as a local editable package:

```bash
conda create -n momoagent -c conda-forge python=3.12 pip pyyaml pybullet -y
conda activate momoagent
python -m pip install -U pip
python -m pip install -e ./sdk lerobot ftservo-python-sdk feetech-servo-sdk numpy scipy kinpy
```

The full project install from the README is also fine:

```bash
python -m pip install -r requirements/advanced.txt -r requirements/nanobot-bridge.txt -e ./sdk
```

Optional serial config:

```bash
export SOARMMOCE_CONFIG=$PWD/sdk/src/soarmmoce_sdk/resources/configs/soarm_moce_serial.yaml
```

## Quick Start

Use the bundled direct helper when the external agent can execute Python commands:

```bash
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py state
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py joint --joint shoulder_pan --delta-deg 3 --duration 1.0 --speed-percent 30
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py move-delta --dz 0.01 --frame base --duration 1.0 --speed-percent 25
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py gripper --open-ratio 1.0
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py home --speed-percent 30
python skills/momo-direct-robot-control/scripts/momo_direct_tool.py stop
```

Use SDK smoke scripts for bring-up:

```bash
python sdk/scripts/0_robot_ping.py
python sdk/scripts/0_robot_get_state.py
python sdk/scripts/1_joint_move.py
python sdk/scripts/1_gripper_control.py
python sdk/scripts/2_cartesian_delta_move.py
```

## Direct SDK Pattern

For agent tools written in Python, use this shape:

```python
from soarmmoce_sdk import SoArmMoceController, resolve_config, to_jsonable

arm = SoArmMoceController(resolve_config(None))
try:
    state = arm.get_state()
    result = arm.move_joint(
        joint="shoulder_pan",
        delta_deg=3.0,
        duration=1.0,
        speed_percent=30,
        wait=True,
        timeout=4.0,
    )
    payload = to_jsonable(result)
finally:
    arm.close(disable_torque=False)
```

Prefer one long-lived controller inside an agent runtime when the agent supports persistent tools. If each tool call is a separate process, use `momo_direct_tool.py`; it opens a controller, runs one command, prints JSON, and closes without releasing torque by default.

## Script Entries

- `skills/momo-direct-robot-control/scripts/momo_direct_tool.py`: direct SDK command tool for external agents.
- `sdk/scripts/0_robot_ping.py`: check serial bus and responding motor IDs.
- `sdk/scripts/0_robot_get_state.py`: read state without intentional register writes.
- `sdk/scripts/1_joint_move.py`: single-joint motion example.
- `sdk/scripts/1_gripper_control.py`: gripper example.
- `sdk/scripts/1_home_stop.py`: home, stop, and torque examples.
- `sdk/scripts/2_cartesian_delta_move.py`: small Cartesian delta example.
- `sdk/scripts/2_cartesian_pose_move.py`: Cartesian pose example.
- `Software/Master/momo_robot_service/main.py`: optional API service, not needed for direct SDK mode.

## Safety

- Read state before motion and after motion.
- Use small defaults: joint deltas <= 5 degrees, Cartesian deltas <= 10 mm, speed <= 30 for first tests.
- Ask one concise clarification question when target joint, direction, coordinate frame, or motion size is ambiguous.
- `move_joint` uses degrees. `get_state()["joint_state"]["q"]` is radians, while named `joint_state` entries are degrees.
- `move_delta` uses meters and radians. `frame="base"` moves in base/world coordinates; `frame="tool"` moves along the current tool frame.
- Keep `disable_torque=False` on normal close so the arm does not suddenly drop.
- Use `arm.stop()` immediately for stop/cancel/emergency wording.
- Never claim a physical action succeeded unless the SDK command returned without error and the after-state is read.
- Do not copy secrets, `.env`, runtime session state, logs, or machine-specific calibration from another robot by accident.

## Pitfalls

- Direct SDK control is the simplest path for a single external agent, but it bypasses the GUI/Web coordination layer.
- The SDK's default config contains a machine-specific serial port. Update `transport.port` or set `SOARMMOCE_CONFIG` when moving machines.
- Cartesian commands require the PyBullet kinematics backend and bundled URDF/mesh resources.
- Home means the calibrated/startup reference used by the rebuilt SDK, not a universal factory pose.
- If another local process already controls the arm, the SDK may report a session lock. Stop the other process or run only one direct-control agent.
- `quick_control_api` is deprecated. Ignore it unless maintaining old commands.
