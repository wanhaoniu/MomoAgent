from __future__ import annotations

import argparse
import json
import logging
import threading
import time

import uvicorn

from face_tracking.config import AppConfig, load_config
from face_tracking.logging_utils import setup_logging
from face_tracking.service import SkillService, create_app


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    if args.source_type:
        config.source.type = args.source_type
    if args.camera_index is not None:
        config.source.camera_index = args.camera_index
    if args.rtsp_url:
        config.source.rtsp_url = args.rtsp_url
    if args.video_path:
        config.source.video_path = args.video_path
    if args.capture_uri:
        config.source.capture_uri = args.capture_uri
    if args.model_backend:
        config.detector.backend = args.model_backend
    if args.model_path:
        config.detector.model_path = args.model_path
    if args.model_name:
        config.detector.model_name = args.model_name
    if args.device:
        config.detector.device = args.device
    if args.show_gui:
        config.visualizer.enabled = True
    if args.headless:
        config.visualizer.enabled = False
    if args.host:
        config.service.host = args.host
    if args.port:
        config.service.port = args.port
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smart mirror face tracking service")
    parser.add_argument("--config", default="configs/default.yaml", help="Path to YAML config file")
    parser.add_argument("--source-type", choices=["camera", "rtsp", "video_file", "capture"])
    parser.add_argument("--camera-index", type=int)
    parser.add_argument("--rtsp-url")
    parser.add_argument("--video-path")
    parser.add_argument("--capture-uri")
    parser.add_argument("--model-backend", choices=["insightface_onnx", "insightface_faceanalysis", "opencv_yunet"])
    parser.add_argument("--model-path")
    parser.add_argument("--model-name")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--show-gui", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    return parser


def run_headless_service(config: AppConfig) -> None:
    service = SkillService(config)
    app = create_app(service, manage_lifecycle=True)
    uvicorn.run(
        app,
        host=config.service.host,
        port=config.service.port,
        log_level=config.logging.level.lower(),
    )


def run_gui_service(config: AppConfig) -> None:
    service = SkillService(config)
    logger = logging.getLogger("face_tracking.main")
    service.start()
    app = create_app(service, manage_lifecycle=False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.service.host,
            port=config.service.port,
            log_level=config.logging.level.lower(),
        )
    )
    server_thread = threading.Thread(target=server.run, name="uvicorn-thread", daemon=True)
    server_thread.start()

    try:
        while not getattr(server, "started", False) and server_thread.is_alive():
            time.sleep(0.05)
        service.run_visualizer_loop()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5.0)
        service.stop()
        logger.info("GUI mode stopped")


def cli_main() -> None:
    args = build_parser().parse_args()
    config = apply_cli_overrides(load_config(args.config), args)
    log_file = setup_logging(config.logging)
    logger = logging.getLogger("face_tracking.main")
    logger.info("Logging to %s", log_file)

    if args.print_config:
        print(json.dumps(config.model_dump(), indent=2, ensure_ascii=False))
        return

    if config.visualizer.enabled:
        logger.info("Starting in GUI mode; OpenCV window will run on the main thread")
        run_gui_service(config)
        return

    logger.info("Starting in headless mode")
    run_headless_service(config)


if __name__ == "__main__":
    cli_main()
