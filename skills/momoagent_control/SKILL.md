---
name: momoagent-control
description: Use when asked to control the MomoAgent robotic arm through quick_control_api on http://127.0.0.1:8010. Covers session connect/disconnect, robot state inspection, joint-step motion, Cartesian jog, home, stop, face/person follow, and idle scan. Prefer these HTTP endpoints instead of opening a second direct hardware session.
---

# MomoAgent Control

Use this skill to control the arm through `quick_control_api` on `http://127.0.0.1:8010`.

Prefer these API endpoints over any legacy direct-SDK control path. The API is the control surface for:

- robot session connect / disconnect
- robot state inspection
- direct motion
- stop
- follow / tracking
- idle scan

Do not use `/api/v1/agent/*` for arm motion. Those are for the text agent, not robot actuation.

## Preconditions

- `quick_control_api` must already be running on `127.0.0.1:8010`.
- If you use shell `curl` on this machine, add `--noproxy "*"` for localhost requests.
- For follow mode, `face_loc` should normally already be running on `127.0.0.1:8000` unless the caller gives another `latest_url`.
- Treat hardware motion as safety-sensitive. If the request is ambiguous, start with a small move and low speed.

## Session Workflow

1. Check service health if needed.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/health
```

2. Check whether the robot session is connected.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/session/status
```

3. If `data.connected` is `false`, connect the real arm.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/session/connect \
  -H 'Content-Type: application/json' \
  -d '{"prefer_real":true,"allow_sim_fallback":false}'
```

4. Only disconnect when the user explicitly asks to disconnect or shut the robot session down.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/session/disconnect
```

## Inspect State

Use this before or after motion when the caller wants current pose or joint data.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/robot/state
```

Useful joint index mapping for `joint-step`:

- `0`: `shoulder_pan`
- `1`: `shoulder_lift`
- `2`: `elbow_flex`
- `3`: `wrist_flex`
- `4`: `wrist_roll`
- `5`: `gripper`

If the user asks for a specific joint by name, convert it to this index mapping.

## Direct Motion

### Joint Step

Use `joint-step` when the user asks for a precise joint adjustment, pan / tilt adjustment, wrist move, or gripper move.

Request shape:

- `joint_index`: `0` to `5`
- `delta_deg`: signed delta in degrees
- `speed_percent`: `1` to `100`, default `50`

Example:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/motion/joint-step \
  -H 'Content-Type: application/json' \
  -d '{"joint_index":0,"delta_deg":5.0,"speed_percent":30}'
```

Guidance:

- If the user says “slightly”, prefer small magnitudes like `2` to `5` degrees.
- If the sign direction is unclear, use a very small step first and verify.
- For gripper open / close, use `joint_index=5` with a small signed step unless the user gave a known convention.

### Cartesian Jog

Use `cartesian-jog` when the user wants the end-effector or camera to move in space rather than rotate a named joint.

Allowed `axis` values:

- `+X`, `-X`, `+Y`, `-Y`, `+Z`, `-Z`
- `+RX`, `-RX`, `+RY`, `-RY`, `+RZ`, `-RZ`

Request shape:

- `axis`: required
- `coord_frame`: `base` or `tool`, default `base`
- `jog_mode`: `step` or `continuous`, default `step`
- `step_dist_mm`: translation amount, default `5.0`
- `step_angle_deg`: rotation amount, default `5.0`
- `speed_percent`: `1` to `100`, default `50`

Example:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/motion/cartesian-jog \
  -H 'Content-Type: application/json' \
  -d '{"axis":"+Y","coord_frame":"base","jog_mode":"step","step_dist_mm":5.0,"step_angle_deg":5.0,"speed_percent":25}'
```

Guidance:

- Prefer `jog_mode="step"` for almost all conversational control requests.
- Use small distances like `2` to `10` mm unless the user explicitly wants a larger move.
- Use `tool` frame only when the user clearly wants motion relative to the tool direction.

### Home

Use `home` when the user asks for home, reset pose, startup pose, or return-to-origin behavior.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/motion/home \
  -H 'Content-Type: application/json' \
  -d '{"source":"home","speed_percent":40}'
```

Allowed `source` values:

- `home`
- `origin`
- `zero`
- `startup`

When unsure, use `home`.

### Stop

Use `stop` immediately if:

- the user says stop, halt, freeze, emergency stop, 停, or 别动
- a motion result looks unsafe or unexpected
- you need to cancel ongoing manual motion

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/motion/stop
```

Important:

- Any manual `/motion/*` command also stops active follow or idle-scan behavior.

## Behavior Modes

### Follow

Use this when the user asks for face follow, person tracking, subject tracking, 主播跟随, or “keep the person in frame”.

Start with a compact payload unless the caller asks for special tuning:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/follow/start \
  -H 'Content-Type: application/json' \
  -d '{"target_kind":"face","enable_idle_scan_fallback":true}'
```

Status:

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/follow/status
```

Stop:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/follow/stop
```

Key request fields:

- `target_kind`: `face`, `person`, or `generic`
- `enable_idle_scan_fallback`: whether to switch into idle scan when the target disappears
- `latest_url`: default is `http://127.0.0.1:8000/latest`

Decision rules:

- Prefer `target_kind="face"` by default.
- Prefer `enable_idle_scan_fallback=true` for demo mode unless the user explicitly wants pure follow only.

### Idle Scan

Use this when the user asks for standby cruise, patrol motion, idle movement, or “don’t keep the camera frozen”.

Start:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/start \
  -H 'Content-Type: application/json' \
  -d '{"pan_joint":"shoulder_pan","tilt_joint":"elbow_flex","speed_percent":25,"pan_range_deg":10.0,"tilt_range_deg":8.0,"move_duration_min_sec":1.2,"move_duration_max_sec":2.8,"dwell_sec_min":0.8,"dwell_sec_max":2.5}'
```

Minimal payload if defaults are fine:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/start \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Status:

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/idle-scan/status
```

Stop:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/stop
```

## Recommended Decision Rules

- If the user asks for a one-off precise adjustment, use `joint-step` or `cartesian-jog`, not follow or idle scan.
- If the user names a joint or gripper, prefer `joint-step`.
- If the user describes movement in workspace terms like left / right / up / down / forward / back / rotate, prefer `cartesian-jog`.
- If the user asks to reset pose, use `home`.
- If the user asks to track a person or face, use `follow/start`.
- If the user asks for passive idle motion, use `idle-scan/start`.
- If the user asks to stop motion immediately, use `motion/stop` first.
- Do not disconnect the session automatically after each move; keep the session alive unless the caller asks to disconnect.

## Notes

- Connect first if any motion request fails with `NOT_CONNECTED`.
- Use `robot/state` to inspect before making a follow-up move when the caller wants precise positioning.
- `follow/start` and `idle-scan/start` replace existing background behavior workers.
- Manual motion endpoints are the safer default for conversational control because they are bounded and easier to reason about than long-running behaviors.
