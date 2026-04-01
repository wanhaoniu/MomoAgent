from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .schemas import CartesianJogRequest, ConnectRequest, HomeRequest, JointStepRequest
from .service import QuickControlError, QuickControlService


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = QuickControlService()
    app.state.quick_control_service = service
    try:
        yield
    finally:
        service.close()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def create_app() -> FastAPI:
    app = FastAPI(title="MomoAgent Quick Control API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(QuickControlError)
    async def quick_control_error_handler(_request: Request, exc: QuickControlError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                },
            },
        )

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            {
                "status": "ok",
                "service": "momoagent-quick-control-api",
                "session": service.session_status(),
            }
        )

    @app.get("/api/v1/session/status")
    async def session_status(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.session_status())

    @app.post("/api/v1/session/connect")
    async def connect(payload: ConnectRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.connect(prefer_real=payload.prefer_real, allow_sim_fallback=payload.allow_sim_fallback))

    @app.post("/api/v1/session/disconnect")
    async def disconnect(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.disconnect())

    @app.get("/api/v1/robot/state")
    async def robot_state(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.robot_state_payload())

    @app.post("/api/v1/motion/joint-step")
    async def motion_joint_step(payload: JointStepRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.joint_step(
                joint_index=payload.joint_index,
                delta_deg=payload.delta_deg,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/cartesian-jog")
    async def motion_cartesian_jog(payload: CartesianJogRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(
            service.cartesian_jog(
                axis=payload.axis,
                coord_frame=payload.coord_frame,
                jog_mode=payload.jog_mode,
                step_dist_mm=payload.step_dist_mm,
                step_angle_deg=payload.step_angle_deg,
                speed_percent=payload.speed_percent,
            )
        )

    @app.post("/api/v1/motion/home")
    async def motion_home(payload: HomeRequest, request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.home(source=payload.source, speed_percent=payload.speed_percent))

    @app.post("/api/v1/motion/stop")
    async def motion_stop(request: Request) -> dict[str, Any]:
        service: QuickControlService = request.app.state.quick_control_service
        return _ok(service.stop())

    @app.websocket("/api/v1/ws/state")
    async def ws_state(websocket: WebSocket):
        await websocket.accept()
        service: QuickControlService = websocket.app.state.quick_control_service
        try:
            while True:
                await websocket.send_json({"type": "state", "data": service.robot_state_payload()})
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    return app


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="MomoAgent Quick Control API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "quick_control_api.app:create_app",
        host=str(args.host),
        port=int(args.port),
        reload=bool(args.reload),
        factory=True,
    )
