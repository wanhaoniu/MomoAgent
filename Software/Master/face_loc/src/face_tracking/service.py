from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

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

    def latest_display_frame(self) -> tuple[Any | None, int]:
        frame, frame_id = self.engine.get_latest_display_frame()
        if frame is None:
            return None, frame_id
        return frame.copy(), frame_id

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

    @app.get("/frame.jpg")
    async def frame_jpg(
        max_width: int = 640,
        quality: int = 72,
    ) -> Response:
        frame, frame_id = await asyncio.to_thread(service.latest_display_frame)
        if frame is None:
            return Response(
                content=b"No frame available yet",
                status_code=503,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )

        width = int(frame.shape[1]) if getattr(frame, "shape", None) is not None else 0
        height = int(frame.shape[0]) if getattr(frame, "shape", None) is not None else 0
        target_width = max(64, min(1920, int(max_width or 640)))
        jpeg_quality = max(35, min(92, int(quality or 72)))
        if width > target_width and width > 0 and height > 0:
            target_height = max(1, int(round(height * target_width / float(width))))
            frame = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)

        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
        )
        if not ok:
            return Response(
                content=b"Failed to encode preview frame",
                status_code=500,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )

        return Response(
            content=encoded.tobytes(),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "X-Frame-Id": str(int(frame_id)),
            },
        )

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
