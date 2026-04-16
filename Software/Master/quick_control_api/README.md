# Quick Control API

Unified local backend for robot control plus OpenClaw agent session management.

## Start

```bash
python /Users/moce/Documents/Project/MomoAgent/Software/Master/quick_control_api/main.py --host 0.0.0.0 --port 8010
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

- Default behavior now targets the rebuilt local serial SDK runtime directly.
- Mock simulation fallback has been removed from this API because the current SDK no longer exposes the old mock transport path.
- Cartesian jog is routed to the rebuilt SDK `move_delta()` path and uses the same base/tool frame semantics as the current shell tools.
- `POST /api/v1/motion/home` accepts `source=home|origin|zero|startup`. All of them map to the rebuilt runtime "startup pose is the reference home" behavior; the field is mainly kept for UI/API compatibility.
- `follow/start` now launches a backend worker that polls `Software/Master/face_loc` `/latest` directly and runs the validated `sdk/tests/face_follow.py` control logic inside the API process. `follow/stop` stops that worker.
- `follow` and `idle_scan` are behavior-layer APIs intended for agent/backend orchestration. Any manual `/motion/*` call will stop both.
- `WS /api/v1/ws/agent-stream` is the recommended frontend entrypoint for text plus optional backend-bridged streaming TTS.
- OpenClaw warm session state is persisted locally so repeated turns do not pay the full cold-start cost every time.
- If your OpenClaw skill still controls hardware by grabbing the SDK directly, it may conflict with an already-connected robot session. The long-term fix is to make skill-side robot actions call this API instead of opening a second hardware session.
- `POST /api/v1/haiguitang/agent/turn` is the dedicated HaiGuiTang scene orchestration API. It forwards the turn to OpenClaw, converts the reply into subtitle/video/action directives, updates the full-screen scene, and triggers robot nod/shake when needed.
