# Robot Embodiment Declaration — MomoAgent Direct SDK

> Profile: momoagent | Driver: MomoAgentDriver

## Identity

- **Name**: MomoAgent robotic arm
- **Type**: Fixed-base 5-DOF SOARM101-derived manipulator with optional gripper
- **Control mode**: Direct local Python SDK through `soarmmoce_sdk`
- **Runtime entry**: `runtime/momo_bridge.py`

## Supported Actions

| Action | Required Parameters | Description |
|---|---|---|
| `preflight` / `momo_preflight` | optional `connect` | Check SDK import, config, and optionally hardware connection |
| `state` / `momo_state` | none | Read joints, TCP pose, gripper state |
| `joint_delta` / `momo_joint_delta` | `joint`, `delta_deg` | Move one joint by degrees |
| `joint_target` / `momo_joint_target` | `joint`, `target_deg` | Move one joint to a target angle |
| `joints_target` / `momo_joints_target` | `targets_deg` | Move multiple joints to target degrees |
| `cartesian_delta` / `momo_cartesian_delta` | any of `dx`, `dy`, `dz`, `drx`, `dry`, `drz` | Small TCP delta move |
| `pose` / `momo_pose` / `move_to` | `xyz`, optional `rpy` | Move TCP to absolute pose |
| `gripper` / `set_gripper` | `open_ratio` | Set gripper ratio, 0 closed and 1 open |
| `open_gripper` | none | Open gripper |
| `close_gripper` | none | Close gripper |
| `home` / `momo_home` | optional `speed_percent` | Move to SDK home/startup reference |
| `stop` / `momo_stop` | none | Hold current raw positions |
| `enable_torque` / `disable_torque` | none | Enable or disable torque |

## Common Parameters

- `duration`: motion duration in seconds.
- `speed_percent`: 1 to 100, default should stay conservative.
- `timeout`: SDK motion timeout in seconds.
- `wait`: whether to wait for completion.
- `config`: optional SDK YAML config path.

## Safety Defaults

- Read `state` before motion when the intended movement depends on current pose.
- Start with joint deltas <= 5 degrees and Cartesian deltas <= 0.01 m.
- Use `speed_percent <= 30` for first tests.
- `move_delta` uses meters and radians.
- `joint_delta` and `joint_target` use degrees.
- Use `stop` immediately for stop/cancel/emergency requests.
- `disable_torque` can make the arm drop; use only when a human is supporting it.

## Environment Variables

- `MOMOAGENT_REPO_ROOT`: optional path to a MomoAgent repo checkout.
- `MOMOAGENT_SDK_SRC`: optional direct path to `sdk/src`.
- `SOARMMOCE_CONFIG`: optional SDK YAML config path.
- `SOARMMOCE_PORT`: optional serial port override.
- `MOMOAGENT_PYTHON`: Python executable used by the PhyAgentOS driver to run the bridge.

## Copy Requirements

The plugin needs `soarmmoce_sdk`. The easiest layout is to keep this plugin inside the MomoAgent repo. For a standalone plugin repository, copy the MomoAgent `sdk/` directory into either:

- `runtime/third_party/MomoAgent/sdk/`
- or the plugin repository root as `sdk/`

Do not copy `.env`, API keys, runtime session state, logs, or calibration from another physical robot unless intentionally cloning that robot setup.
