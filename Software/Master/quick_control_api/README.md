# Quick Control API

Minimal FastAPI skeleton for the Quick Move page only.

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
- `WS /api/v1/ws/state`

## Notes

- Default behavior now targets the rebuilt local serial SDK runtime directly.
- Mock simulation fallback has been removed from this API because the current SDK no longer exposes the old mock transport path.
- Cartesian jog is routed to the rebuilt SDK `move_delta()` path and uses the same base/tool frame semantics as the current shell tools.
- `POST /api/v1/motion/home` accepts `source=home|origin|zero|startup`. All of them map to the rebuilt runtime "startup pose is the reference home" behavior; the field is mainly kept for UI/API compatibility.
- This skeleton only targets the Quick Move page; Job/recording APIs are intentionally excluded.
