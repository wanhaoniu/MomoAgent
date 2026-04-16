# HaiGuiTang Web

This folder contains the dedicated HaiGuiTang web scene.

Recommended way to open it:

1. Start `quick_control_api`
2. Open `http://<host>:8010/web/`

For convenience, the API also redirects:

- `http://<host>:8010/haiguitang`

The page is intentionally same-origin with the API so it can:

- load `GET /api/v1/scenes/haiguitang/config`
- poll `GET /api/v1/scenes/haiguitang/state`
- send `POST /api/v1/scenes/haiguitang/state`
- send `POST /api/v1/haiguitang/agent/turn`
- trigger `POST /api/v1/haiguitang/start`
- trigger `POST /api/v1/haiguitang/act`

Media files should live in:

`Software/Master/quick_control_api/runtime/media`

The console now includes an Agent section for manual testing. The same `POST /api/v1/haiguitang/agent/turn`
endpoint is intended to be reused later by STT or any external OpenClaw-driven controller.
