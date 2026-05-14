# Momo Robot Service

Unified local backend for robot control, state, behavior workers, and agent session management.

## Start

```bash
python /Users/moce/Documents/Project/MomoAgent/Software/Master/momo_robot_service/main.py --host 0.0.0.0 --port 8010
```

## Endpoints

- `GET /api/v1/health`
- `GET /api/v1/session/status`
- `POST /api/v1/session/connect`
- `POST /api/v1/session/disconnect`
- `GET /api/v1/robot/state`
- `POST /api/v1/motion/joint-step`
- `POST /api/v1/motion/cartesian-jog`
- `POST /api/v1/motion/home`
- `POST /api/v1/motion/stop`
- `POST /api/v1/motion/free-move`
- `POST /api/v1/tools/dispatch`
- `GET /api/v1/follow/status`
- `POST /api/v1/follow/start`
- `POST /api/v1/follow/stop`
- `GET /api/v1/idle-scan/status`
- `POST /api/v1/idle-scan/start`
- `POST /api/v1/idle-scan/stop`
- `GET /api/v1/haiguitang/status`
- `GET /api/v1/scenes/haiguitang/config`
- `GET /api/v1/scenes/haiguitang/state`
- `POST /api/v1/scenes/haiguitang/state`
- `POST /api/v1/haiguitang/start`
- `POST /api/v1/haiguitang/act`
- `POST /api/v1/haiguitang/stop`
- `POST /api/v1/haiguitang/agent/turn`
- `GET /api/v1/agent/status`
- `GET /api/v1/agent/last-turn`
- `POST /api/v1/agent/warmup`
- `POST /api/v1/agent/reset-session`
- `POST /api/v1/agent/ask`
- `WS /api/v1/ws/state`
- `WS /api/v1/ws/agent`
- `WS /api/v1/ws/agent-stream`

## Notes

- `momo_robot_service` is the intended single owner of the real robot SDK session.
- Default behavior targets the rebuilt local serial SDK runtime directly.
- Mock simulation fallback has been removed from this API because the current SDK no longer exposes the old mock transport path.
- Cartesian jog is routed to the rebuilt SDK `move_delta()` path and uses the same base/tool frame semantics as the current shell tools.
- `POST /api/v1/motion/home` accepts `source=home|origin|zero|startup`. All of them map to the rebuilt runtime "startup pose is the reference home" behavior; the field is mainly kept for UI/API compatibility.
- `follow/start` now launches a backend worker that polls `Software/Master/face_loc` `/latest` directly and runs the validated `sdk/tests/face_follow.py` control logic inside the API process. `follow/stop` stops that worker.
- `follow` and `idle_scan` are behavior-layer APIs intended for agent/backend orchestration. Any manual `/motion/*` call will stop both.
- `POST /api/v1/tools/dispatch` is the unified Nanobot/tool bridge. It uses the same in-process robot tool implementation as the service-side agent, so GUI/Web/App/Nanobot do not need separate hardware sessions.
- `WS /api/v1/ws/agent-stream` is the recommended frontend entrypoint for text plus optional backend-bridged streaming TTS.
- Nanobot is now the default text-agent backend. Configure it with `MOMO_AGENT_NANOBOT_*`; if those are not set, model/API settings fall back to the existing `AUTOGRASP_VLM_*` variables in `.env`.
- `POST /api/v1/haiguitang/agent/turn` is the dedicated HaiGuiTang scene orchestration API. It runs the configured text agent, converts the reply into subtitle/video/action directives, updates the full-screen scene, and triggers robot nod/shake when needed.
