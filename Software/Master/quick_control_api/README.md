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

- Default behavior prefers the real serial config and can fall back to mock simulation.
- This skeleton only targets the Quick Move page; Job/recording APIs are intentionally excluded.
