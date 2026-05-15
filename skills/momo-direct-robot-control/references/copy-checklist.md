# Copy Checklist

Use this checklist when giving another agent enough of this repository to directly control the MomoAgent arm.

## Minimal Direct SDK Copy

Preserve this layout:

```text
repo/
  sdk/
  skills/momo-direct-robot-control/
```

Required:

- `sdk/pyproject.toml`
- `sdk/src/soarmmoce_sdk/`
- `skills/momo-direct-robot-control/`

Recommended smoke scripts:

- `sdk/scripts/_robot_script_common.py`
- `sdk/scripts/0_robot_ping.py`
- `sdk/scripts/0_robot_get_state.py`
- `sdk/scripts/1_joint_move.py`
- `sdk/scripts/1_gripper_control.py`
- `sdk/scripts/1_home_stop.py`
- `sdk/scripts/2_cartesian_delta_move.py`
- `sdk/scripts/2_cartesian_pose_move.py`

Optional docs for humans:

- `README.md`
- `README_ZH.md`
- `docs/0基础机械臂快速入门.md`

## If the Agent Also Needs Existing Tool Schemas

Copy:

- `Software/Master/hmi/skills_dispatcher.py`

This file contains the older OpenAI-style robot tool schema names. For direct SDK mode, prefer writing a small native tool wrapper around `soarmmoce_sdk` instead of routing through `momo_robot_service`.

## If the Agent Also Needs Built-In Nanobot Voice/Text

Copy:

- `Software/Master/momo_agent/`
- `requirements/nanobot-bridge.txt`

This is optional. Direct SDK control does not require `momo_agent`.

## If the Agent Also Needs HTTP/API/GUI/Web

Copy:

- `Software/Master/momo_robot_service/`
- `Software/Web/`
- `requirements/base.txt`
- `requirements/advanced.txt`

This is a separate integration mode. Do not choose it just to let one external agent control the arm.

## Install Command

From the copied repo root:

```bash
conda create -n momoagent -c conda-forge python=3.12 pip pyyaml pybullet -y
conda activate momoagent
python -m pip install -U pip
python -m pip install -e ./sdk lerobot ftservo-python-sdk feetech-servo-sdk numpy scipy kinpy
```

Full project install also works:

```bash
python -m pip install -r requirements/advanced.txt -r requirements/nanobot-bridge.txt -e ./sdk
```

## Do Not Copy

- `.env`, `env`, API keys, or machine-specific secrets.
- `__pycache__/`, `.pytest_cache/`, `.DS_Store`, logs, or generated runtime session files.
- `Software/Master/momo_agent/runtime/nanobot_session_state.json`.
- Machine-specific calibration/config from a different robot unless deliberately cloning that exact setup.

## Port and Config

The default serial config is:

```text
sdk/src/soarmmoce_sdk/resources/configs/soarm_moce_serial.yaml
```

Check or edit:

```yaml
transport:
  port: /dev/tty.usbmodem5B140317411
```

Alternative:

```bash
export SOARMMOCE_CONFIG=/absolute/path/to/soarm_moce_serial.yaml
```
