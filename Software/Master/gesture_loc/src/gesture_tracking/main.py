from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from gesture_tracking.config import load_config
from gesture_tracking.service import GestureService, create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MediaPipe gesture tracking service")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[2] / "configs" / "default.yaml"),
        help="Path to YAML config file",
    )
    parser.add_argument("--host", default=None, help="Override service host")
    parser.add_argument("--port", type=int, default=None, help="Override service port")
    parser.add_argument("--visualizer", action="store_true", help="Enable local OpenCV preview window")
    return parser


def cli_main() -> None:
    args = _build_parser().parse_args()
    config = load_config(args.config)
    if args.host:
        config.service.host = str(args.host)
    if args.port is not None:
        config.service.port = int(args.port)
    if args.visualizer:
        config.visualizer.enabled = True

    log_dir = Path(config.logging.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, str(config.logging.level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    service = GestureService(config)
    app = create_app(service)
    uvicorn.run(app, host=config.service.host, port=int(config.service.port), log_level="info")
