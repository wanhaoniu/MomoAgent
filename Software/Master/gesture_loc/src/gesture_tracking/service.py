from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from gesture_tracking.config import AppConfig
from gesture_tracking.engine import GestureTrackingEngine


class GestureService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.engine = GestureTrackingEngine(config)

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


def create_app(service: GestureService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(title=service.config.app_name, version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        status = service.status_payload()
        return {
            "status": "ok" if status["running"] else "degraded",
            "engine_running": status["running"],
            "last_error": status["last_error"],
            "fps": status["fps"],
            "latest_gesture": status["latest_gesture"],
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

    return app
