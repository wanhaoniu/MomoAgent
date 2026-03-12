from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from face_tracking.config import AppConfig
from face_tracking.engine import TrackingEngine


class SkillService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.engine = TrackingEngine(config)

    def start(self) -> None:
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()

    def latest_result(self) -> dict[str, Any]:
        return self.engine.get_latest_result()

    def status_payload(self) -> dict[str, Any]:
        return self.engine.get_status()

    def config_payload(self) -> dict[str, Any]:
        return self.config.model_dump()

    def wait_for_newer_result(self, last_version: int, timeout: float) -> tuple[dict[str, Any], int]:
        return self.engine.wait_for_newer_result(last_version, timeout)


    def run_visualizer_loop(self, poll_interval_sec: float = 0.01) -> None:
        self.engine.run_visualizer_loop(poll_interval_sec=poll_interval_sec)


def create_app(service: SkillService, manage_lifecycle: bool = True) -> FastAPI:
    if manage_lifecycle:
        @asynccontextmanager
        async def lifespan(_: FastAPI):
            service.start()
            try:
                yield
            finally:
                service.stop()
    else:
        lifespan = None

    app = FastAPI(title=service.config.app_name, version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        status = service.status_payload()
        return {
            "status": "ok" if status["running"] else "degraded",
            "engine_running": status["running"],
            "last_error": status["last_error"],
            "fps": status["fps"],
        }

    @app.get("/latest")
    async def latest() -> dict[str, Any]:
        return service.latest_result()

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return service.status_payload()

    @app.get("/config")
    async def config() -> dict[str, Any]:
        return service.config_payload()

    @app.websocket(service.config.service.websocket_path)
    async def websocket_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        version = -1
        try:
            while True:
                payload, version = await asyncio.to_thread(
                    service.wait_for_newer_result,
                    version,
                    service.config.service.ws_interval_sec,
                )
                await websocket.send_json(payload)
                await asyncio.sleep(service.config.service.ws_interval_sec)
        except WebSocketDisconnect:
            return

    return app
