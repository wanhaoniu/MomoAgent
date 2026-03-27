from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "runtime" / "openclaw_face_tracking_state.json"
LOG_PATH = REPO_ROOT / "logs" / "openclaw_face_tracking.log"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "default.yaml"


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def ensure_runtime_paths() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_state(payload: dict[str, Any]) -> None:
    ensure_runtime_paths()
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


def is_process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def http_get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(url: str, timeout_sec: float = 20.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            payload = http_get_json(url, timeout=2.0)
            if payload.get("status") in {"ok", "degraded"}:
                return payload
        except Exception:
            pass
        time.sleep(0.5)
    return None


def normalize_query_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw skill wrapper for smart mirror face tracking")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start_face_tracking")
    start_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    start_parser.add_argument("--source-type", choices=["camera", "rtsp", "video_file", "capture"])
    start_parser.add_argument("--camera-index", type=int)
    start_parser.add_argument("--camera-name")
    start_parser.add_argument("--rtsp-url")
    start_parser.add_argument("--video-path")
    start_parser.add_argument("--capture-uri")
    start_parser.add_argument("--model-backend", choices=["insightface_onnx", "insightface_faceanalysis", "opencv_yunet"])
    start_parser.add_argument("--model-path")
    start_parser.add_argument("--model-name")
    start_parser.add_argument("--device", choices=["auto", "cpu", "cuda"])
    start_parser.add_argument("--host", default="127.0.0.1")
    start_parser.add_argument("--port", type=int, default=8000)
    start_parser.add_argument("--show-gui", action="store_true")
    start_parser.add_argument("--headless", action="store_true")

    subparsers.add_parser("get_face_tracking_result")
    subparsers.add_parser("get_face_tracking_status")
    subparsers.add_parser("stop_face_tracking")
    return parser


def action_start(args: argparse.Namespace) -> None:
    ensure_runtime_paths()
    state = load_state()
    if state and is_process_alive(state.get("pid")):
        query_host = state.get("query_host", "127.0.0.1")
        port = state.get("port", 8000)
        try:
            latest_status = http_get_json(f"http://{query_host}:{port}/status")
        except Exception:
            latest_status = {"running": True, "warning": "Process exists but status endpoint is not reachable"}
        emit(
            {
                "ok": True,
                "action": "start_face_tracking",
                "already_running": True,
                "pid": state.get("pid"),
                "host": state.get("host"),
                "port": port,
                "status": latest_status,
            }
        )

    host = args.host
    query_host = normalize_query_host(host)
    cmd = [
        sys.executable,
        "-m",
        "face_tracking.main",
        "--config",
        args.config,
        "--host",
        host,
        "--port",
        str(args.port),
    ]

    for key, value in [
        ("--source-type", args.source_type),
        ("--camera-index", args.camera_index),
        ("--camera-name", args.camera_name),
        ("--rtsp-url", args.rtsp_url),
        ("--video-path", args.video_path),
        ("--capture-uri", args.capture_uri),
        ("--model-backend", args.model_backend),
        ("--model-path", args.model_path),
        ("--model-name", args.model_name),
        ("--device", args.device),
    ]:
        if value is not None:
            cmd.extend([key, str(value)])

    if args.show_gui:
        cmd.append("--show-gui")
    if args.headless:
        cmd.append("--headless")

    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"

    log_handle = LOG_PATH.open("a", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        start_new_session=True,
        env=env,
    )

    health = wait_for_health(f"http://{query_host}:{args.port}/health")
    if health is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        emit(
            {
                "ok": False,
                "action": "start_face_tracking",
                "error": "Service did not become healthy before timeout",
                "log_file": str(LOG_PATH),
                "command": cmd,
            },
            exit_code=1,
        )

    payload = {
        "pid": process.pid,
        "host": host,
        "query_host": query_host,
        "port": args.port,
        "config": str(Path(args.config).resolve()),
        "command": cmd,
        "started_at": time.time(),
        "source_type": args.source_type,
        "model_backend": args.model_backend,
    }
    save_state(payload)
    emit(
        {
            "ok": True,
            "action": "start_face_tracking",
            "already_running": False,
            "pid": process.pid,
            "host": host,
            "port": args.port,
            "health": health,
            "config": payload,
            "log_file": str(LOG_PATH),
        }
    )


def action_latest() -> None:
    state = load_state()
    if not state or not is_process_alive(state.get("pid")):
        clear_state()
        emit(
            {
                "ok": False,
                "action": "get_face_tracking_result",
                "error": "Tracking service is not running",
            },
            exit_code=1,
        )

    url = f"http://{state['query_host']}:{state['port']}/latest"
    try:
        payload = http_get_json(url)
    except URLError as exc:
        emit(
            {
                "ok": False,
                "action": "get_face_tracking_result",
                "error": f"Failed to query latest result: {exc}",
            },
            exit_code=1,
        )
    emit({"ok": True, "action": "get_face_tracking_result", "result": payload})


def action_status() -> None:
    state = load_state()
    if not state or not is_process_alive(state.get("pid")):
        clear_state()
        emit(
            {
                "ok": True,
                "action": "get_face_tracking_status",
                "running": False,
                "error": None,
            }
        )

    url = f"http://{state['query_host']}:{state['port']}/status"
    try:
        payload = http_get_json(url)
    except URLError as exc:
        emit(
            {
                "ok": True,
                "action": "get_face_tracking_status",
                "running": True,
                "warning": f"Process exists but status endpoint is unavailable: {exc}",
                "pid": state.get("pid"),
                "host": state.get("host"),
                "port": state.get("port"),
            }
        )

    emit(
        {
            "ok": True,
            "action": "get_face_tracking_status",
            "running": bool(payload.get("running")),
            "status": payload,
            "pid": state.get("pid"),
            "host": state.get("host"),
            "port": state.get("port"),
        }
    )


def action_stop() -> None:
    state = load_state()
    if not state:
        emit({"ok": True, "action": "stop_face_tracking", "stopped": False, "reason": "Service is not running"})

    pid = state.get("pid")
    if not is_process_alive(pid):
        clear_state()
        emit({"ok": True, "action": "stop_face_tracking", "stopped": False, "reason": "State existed but process is gone"})

    forced = False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError as exc:
        clear_state()
        emit(
            {
                "ok": False,
                "action": "stop_face_tracking",
                "stopped": False,
                "error": f"Failed to send SIGTERM: {exc}",
            },
            exit_code=1,
        )

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not is_process_alive(pid):
            clear_state()
            emit({"ok": True, "action": "stop_face_tracking", "stopped": True, "forced": forced})
        time.sleep(0.25)

    try:
        os.killpg(pid, signal.SIGKILL)
        forced = True
    except OSError:
        pass
    clear_state()
    emit({"ok": True, "action": "stop_face_tracking", "stopped": True, "forced": forced})


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "start_face_tracking":
        action_start(args)
    if args.command == "get_face_tracking_result":
        action_latest()
    if args.command == "get_face_tracking_status":
        action_status()
    if args.command == "stop_face_tracking":
        action_stop()


if __name__ == "__main__":
    main()
