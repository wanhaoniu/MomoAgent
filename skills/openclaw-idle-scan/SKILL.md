---
name: openclaw-idle-scan
description: Use when asked to start, stop, or inspect idle scan, standby cruise, patrol motion, or smooth random camera motion for the MomoAgent arm through quick_control_api. Connect the robot session if needed, then call the idle-scan endpoints.
---

# Idle Scan

Use this skill to run slow standby motion through `quick_control_api` on `http://127.0.0.1:8010`.

## Preconditions

- `quick_control_api` must already be running on `127.0.0.1:8010`.
- If you use shell `curl` on this machine, add `--noproxy "*"` for localhost requests.

## Workflow

1. Check whether the robot session is already connected.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/session/status
```

2. If `data.connected` is `false`, connect the real arm.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/session/connect \
  -H 'Content-Type: application/json' \
  -d '{"prefer_real":true,"allow_sim_fallback":false}'
```

3. Start idle scan.

The current robot pose becomes the anchor pose. The worker then makes smooth random moves around that anchor.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/start \
  -H 'Content-Type: application/json' \
  -d '{"pan_joint":"shoulder_pan","tilt_joint":"elbow_flex","speed_percent":25,"pan_range_deg":10.0,"tilt_range_deg":8.0,"move_duration_min_sec":1.2,"move_duration_max_sec":2.8,"dwell_sec_min":0.8,"dwell_sec_max":2.5}'
```

If the defaults are fine, this also works:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/start \
  -H 'Content-Type: application/json' \
  -d '{}'
```

4. Inspect status when needed.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/idle-scan/status
```

Important fields:

- `data.control_mode`: should be `idle_scan` while the worker is active.
- `data.idle_scan.phase`: current planner phase such as `moving` or `dwell`.
- `data.idle_scan.anchor_pan_deg` and `data.idle_scan.anchor_tilt_deg`: the anchor pose used for scan motion.
- `data.idle_scan.current_target_pan_deg` and `data.idle_scan.current_target_tilt_deg`: the active random target.
- `data.idle_scan.last_error`: backend-side idle-scan error, if any.

5. Stop idle scan when the user asks to stop standby motion.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/idle-scan/stop
```

## Decision Rules

- Use this skill when the user asks for standby cruise, idle movement, patrol motion, or “don’t keep the camera frozen”.
- The current pose becomes the scan center, so move the arm to the desired standby pose before starting idle scan if the anchor matters.
- If the user wants to track faces when visible and only idle-scan when nobody is present, do not stack two separate behaviors. Use `openclaw-face-follow` with `enable_idle_scan_fallback=true`.

## Notes

- `idle-scan/start` automatically replaces any currently running follow or idle-scan worker.
- If the request fails with `NOT_CONNECTED`, connect the session first.
- This behavior does not need `face_loc`; it only needs the quick control API and a connected robot session.
