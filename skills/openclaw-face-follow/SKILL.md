---
name: openclaw-face-follow
description: Use when asked to start, stop, or inspect face follow, person tracking, human tracking, or subject tracking for the MomoAgent arm through quick_control_api. Connect the robot session if needed, then call the follow endpoints. Prefer attention mode with idle-scan fallback enabled for demos where the arm should keep moving when no face is visible.
---

# Face Follow

Use this skill to control tracking behavior through `quick_control_api` on `http://127.0.0.1:8010`.

## Preconditions

- `quick_control_api` must already be running on `127.0.0.1:8010`.
- `face_loc` must already be running on `127.0.0.1:8000`.
- The default tracking source is `http://127.0.0.1:8000/latest`.
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

3. Start follow.

Use this by default for demos. It enables attention mode: track a face when one is visible, briefly hold when the target disappears, then switch into idle scan fallback.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/follow/start \
  -H 'Content-Type: application/json' \
  -d '{"target_kind":"face","enable_idle_scan_fallback":true}'
```

If the user explicitly wants pure face follow without idle behavior, disable fallback:

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/follow/start \
  -H 'Content-Type: application/json' \
  -d '{"target_kind":"face","enable_idle_scan_fallback":false}'
```

4. Inspect status when needed.

```bash
curl --noproxy "*" http://127.0.0.1:8010/api/v1/follow/status
```

Important fields:

- `data.control_mode`: `attention` means face follow with idle-scan fallback; `follow` means pure follow.
- `data.follow.mode`: commonly `tracking`, `hold`, or `scanning`.
- `data.follow.target_visible`: whether a target is currently visible.
- `data.follow.last_error`: backend-side follow error, if any.
- `data.follow.last_result_status`: latest status from the `/latest` observation source.

5. Stop follow when the user asks to stop tracking.

```bash
curl --noproxy "*" -X POST http://127.0.0.1:8010/api/v1/follow/stop
```

## Decision Rules

- Use this skill when the user asks for face follow, person follow, subject tracking, 主播跟随, or “keep the person in frame”.
- Prefer `target_kind="face"` unless the caller explicitly asks for person or generic target tracking.
- Prefer `enable_idle_scan_fallback=true` for demo mode because it avoids a dead still camera when nobody is in frame.
- If the user only wants standby cruise and not tracking, use `openclaw-idle-scan` instead.

## Notes

- `follow/start` automatically replaces any currently running follow or idle-scan worker.
- The backend already has tuned defaults, so a small JSON payload is usually enough.
- If the follow request fails with `NOT_CONNECTED`, connect the session first.
- If the worker is running but the arm is not reacting, inspect `data.follow.last_error` and `data.follow.last_result_status`.
