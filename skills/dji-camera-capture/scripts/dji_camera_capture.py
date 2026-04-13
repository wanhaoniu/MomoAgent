#!/usr/bin/env python3
"""Control macOS camera capture through a native AVFoundation helper."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_ROOT / "workspace"
CAPTURES_DIR = WORKSPACE_DIR / "captures"
RUNTIME_DIR = WORKSPACE_DIR / "runtime"
STATE_PATH = RUNTIME_DIR / "recording_state.json"
LAST_STATE_PATH = RUNTIME_DIR / "last_recording_state.json"
PREFERRED_CAMERA_PATH = RUNTIME_DIR / "preferred_camera.json"
AUTO_PREFERRED_CAMERA_NAME = "OsmoPocket3"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30
NATIVE_SOURCE_PATH = SKILL_ROOT / "scripts" / "dji_camera_native.swift"
NATIVE_BINARY_PATH = RUNTIME_DIR / "dji_camera_native"


class CaptureError(RuntimeError):
    """Raised when camera control fails."""


def ensure_dirs() -> None:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip())
    text = text.strip("-.")
    return text or now_stamp()


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def tail_text(path: Path, max_lines: int = 40) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def load_preferred_camera() -> dict[str, Any] | None:
    if not PREFERRED_CAMERA_PATH.exists():
        return None
    try:
        data = json.loads(PREFERRED_CAMERA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CaptureError(f"Failed to parse preferred camera file: {PREFERRED_CAMERA_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise CaptureError(f"Preferred camera file is malformed: {PREFERRED_CAMERA_PATH}")
    return data


def save_preferred_camera(device: dict[str, Any], *, selection_reason: str) -> None:
    ensure_dirs()
    payload = {
        "name": str(device.get("name") or "").strip(),
        "unique_id": str(device.get("unique_id") or "").strip(),
        "index": int(device.get("index", 0) or 0),
        "selection_reason": selection_reason,
        "saved_at": iso_now(),
    }
    PREFERRED_CAMERA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise CaptureError("`ffmpeg` was not found in PATH. It is needed for MOV -> MP4 remux on stop-video.")
    return path


def run_command(
    cmd: list[str],
    *,
    timeout_sec: float | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"text": True, "check": False}
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.run(cmd, timeout=timeout_sec, **kwargs)


def process_stat(pid: int) -> str:
    if pid <= 0:
        return ""
    result = run_command(["ps", "-o", "stat=", "-p", str(pid)], timeout_sec=5, capture_output=True)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def process_is_alive(pid: int) -> bool:
    stat = process_stat(pid)
    if not stat:
        return False
    return "Z" not in stat


def load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CaptureError(f"Failed to parse state file: {STATE_PATH}: {exc}") from exc


def write_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state(*, save_last: bool = True) -> dict[str, Any] | None:
    state = load_state()
    if state and save_last:
        LAST_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    return state


def stale_or_active_state() -> tuple[dict[str, Any] | None, bool]:
    state = load_state()
    if not state:
        return None, False
    pid = int(state.get("pid", 0) or 0)
    if pid > 0 and process_is_alive(pid):
        return state, True
    clear_state(save_last=True)
    return state, False


def create_session_dirs(session: str | None) -> tuple[str, Path, Path, Path]:
    session_name = slugify(session or now_stamp())
    session_dir = CAPTURES_DIR / session_name
    photos_dir = session_dir / "photos"
    videos_dir = session_dir / "videos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)
    return session_name, session_dir, photos_dir, videos_dir


def ensure_native_helper() -> Path:
    ensure_dirs()
    if not NATIVE_SOURCE_PATH.exists():
        raise CaptureError(f"Native helper source is missing: {NATIVE_SOURCE_PATH}")
    if NATIVE_BINARY_PATH.exists() and NATIVE_BINARY_PATH.stat().st_mtime >= NATIVE_SOURCE_PATH.stat().st_mtime:
        return NATIVE_BINARY_PATH
    cmd = [
        "xcrun",
        "swiftc",
        "-O",
        "-framework",
        "AVFoundation",
        str(NATIVE_SOURCE_PATH),
        "-o",
        str(NATIVE_BINARY_PATH),
    ]
    result = run_command(cmd, timeout_sec=120, capture_output=True)
    if result.returncode != 0 or not NATIVE_BINARY_PATH.exists():
        raise CaptureError(
            "Failed to compile native AVFoundation helper.\n"
            f"{(result.stdout or '').strip()}"
        )
    return NATIVE_BINARY_PATH


def parse_helper_json(output: str) -> dict[str, Any]:
    text = (output or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception as exc:
        raise CaptureError(f"Failed to parse native helper output.\n{text}") from exc


def invoke_native_helper(args: list[str], *, timeout_sec: float) -> dict[str, Any]:
    binary = ensure_native_helper()
    cmd = [str(binary), *args]
    try:
        result = run_command(cmd, timeout_sec=timeout_sec, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("Native helper timed out.") from exc
    payload = parse_helper_json(result.stdout or "")
    if result.returncode != 0:
        error_message = str(payload.get("error") or (result.stdout or "").strip() or "native helper failed")
        raise CaptureError(error_message)
    return payload


def require_device_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = payload.get(key)
    if not isinstance(items, list):
        raise CaptureError(f"Native helper payload is missing '{key}'.")
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def match_device_by_index(devices: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    for item in devices:
        if int(item.get("index", -1) or -1) == int(index):
            return item
    return None


def match_device_by_name(devices: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    requested = normalize_name(name)
    if not requested:
        return None
    for item in devices:
        if normalize_name(str(item.get("name") or "")) == requested:
            return item
    for item in devices:
        if requested in normalize_name(str(item.get("name") or "")):
            return item
    return None


def match_device_by_unique_id(devices: list[dict[str, Any]], unique_id: str) -> dict[str, Any] | None:
    needle = str(unique_id or "").strip()
    if not needle:
        return None
    for item in devices:
        if str(item.get("unique_id") or "").strip() == needle:
            return item
    return None


def resolve_auto_video_device(devices: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    preferred = load_preferred_camera()
    if preferred:
        matched = match_device_by_unique_id(devices, str(preferred.get("unique_id") or ""))
        if matched is not None:
            return matched, "preferred-camera"
        preferred_name = str(preferred.get("name") or "").strip()
        if preferred_name:
            matched = match_device_by_name(devices, preferred_name)
            if matched is not None:
                return matched, "preferred-camera-name"
    favorite = match_device_by_name(devices, AUTO_PREFERRED_CAMERA_NAME)
    if favorite is not None:
        return favorite, "preferred-osmo-pocket3"
    if len(devices) == 1:
        return devices[0], "single-camera"
    return None, "ambiguous"


def resolve_video_device(
    devices: list[dict[str, Any]],
    *,
    camera_name: str,
    video_index: int | None,
    command_label: str,
) -> tuple[dict[str, Any], str]:
    if video_index is not None:
        matched = match_device_by_index(devices, video_index)
        if matched is None:
            available = ", ".join(f"[{item['index']}] {item['name']}" for item in devices) or "<none>"
            raise CaptureError(f"Video index {video_index} was not found. Available cameras: {available}")
        return matched, "explicit-index"
    if str(camera_name or "").strip():
        matched = match_device_by_name(devices, camera_name)
        if matched is None:
            available = ", ".join(f"[{item['index']}] {item['name']}" for item in devices) or "<none>"
            raise CaptureError(f"Camera '{camera_name}' was not found. Available cameras: {available}")
        return matched, "explicit-name"
    matched, reason = resolve_auto_video_device(devices)
    if matched is not None:
        return matched, reason
    available = ", ".join(f"[{item['index']}] {item['name']}" for item in devices) or "<none>"
    raise CaptureError(
        f"Multiple cameras are available and no default camera has been selected for `{command_label}`.\n"
        f"Available cameras: {available}\n"
        "Run `python3 skills/dji-camera-capture/scripts/dji_camera_capture.py list` to inspect devices, "
        "then choose one with `select-camera --camera-name ...`."
    )


def resolve_audio_device(
    devices: list[dict[str, Any]],
    *,
    selected_video_device: dict[str, Any],
    audio_index: int | None,
) -> dict[str, Any]:
    if audio_index is not None:
        matched = match_device_by_index(devices, audio_index)
        if matched is None:
            available = ", ".join(f"[{item['index']}] {item['name']}" for item in devices) or "<none>"
            raise CaptureError(f"Audio index {audio_index} was not found. Available audio devices: {available}")
        return matched
    video_name = str(selected_video_device.get("name") or "").strip()
    matched = match_device_by_name(devices, video_name)
    if matched is not None:
        return matched
    available = ", ".join(f"[{item['index']}] {item['name']}" for item in devices) or "<none>"
    raise CaptureError(
        f"No matching audio device was found for camera '{video_name}'. "
        f"Available audio devices: {available}"
    )


def resolve_devices_for_capture(
    *,
    camera_name: str,
    video_index: int | None,
    with_audio: bool = False,
    audio_index: int | None = None,
    command_label: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, str, dict[str, Any]]:
    payload = invoke_native_helper(["list"], timeout_sec=20)
    video_devices = require_device_list(payload, "video_devices")
    audio_devices = require_device_list(payload, "audio_devices")
    video_device, selection_reason = resolve_video_device(
        video_devices,
        camera_name=camera_name,
        video_index=video_index,
        command_label=command_label,
    )
    audio_device: dict[str, Any] | None = None
    if with_audio:
        audio_device = resolve_audio_device(audio_devices, selected_video_device=video_device, audio_index=audio_index)
    return video_device, audio_device, selection_reason, payload


def spawn_native_record(cmd: list[str], log_path: Path) -> subprocess.Popen[str]:
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    return process


def stop_process_group(pid: int, pgid: int | None = None, timeout_sec: float = 12.0) -> None:
    if pid <= 0:
        raise CaptureError("Invalid process id.")
    group_id = int(pgid or pid)
    if not process_is_alive(pid):
        return
    try:
        os.killpg(group_id, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.time() + max(2.0, float(timeout_sec))
    while time.time() < deadline:
        if not process_is_alive(pid):
            return
        time.sleep(0.25)
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(1.0)
    if not process_is_alive(pid):
        return
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    time.sleep(0.5)
    if process_is_alive(pid):
        raise CaptureError("Failed to stop native recorder cleanly.")


def wait_for_ready(process: subprocess.Popen[str], ready_path: Path, log_path: Path, timeout_sec: float = 12.0) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if ready_path.exists():
            payload = parse_helper_json(ready_path.read_text(encoding="utf-8"))
            ready_path.unlink(missing_ok=True)
            return payload
        if process.poll() is not None:
            details = tail_text(log_path)
            raise CaptureError(
                "Native recorder exited before recording started.\n"
                f"{details.strip()}"
            )
        time.sleep(0.2)
    stop_process_group(process.pid, process.pid, timeout_sec=6.0)
    details = tail_text(log_path)
    raise CaptureError(
        "Timed out while waiting for native recorder readiness.\n"
        f"{details.strip()}"
    )


def remux_mov_to_mp4(raw_path: Path, final_path: Path) -> tuple[Path, str]:
    if not raw_path.exists():
        raise CaptureError(f"Raw recording file is missing: {raw_path}")
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(raw_path),
        "-c",
        "copy",
        str(final_path),
    ]
    result = run_command(cmd, timeout_sec=120, capture_output=True)
    if result.returncode != 0 or not final_path.exists() or final_path.stat().st_size <= 0:
        return raw_path, (result.stdout or "").strip()
    raw_path.unlink(missing_ok=True)
    return final_path, ""


def list_devices() -> dict[str, Any]:
    payload = invoke_native_helper(["list"], timeout_sec=20)
    video_devices = require_device_list(payload, "video_devices")
    default_device, selection_reason = resolve_auto_video_device(video_devices)
    preferred_device: dict[str, Any] | None = None
    preferred = load_preferred_camera()
    if preferred:
        preferred_device = match_device_by_unique_id(video_devices, str(preferred.get("unique_id") or ""))
        if preferred_device is None:
            preferred_name = str(preferred.get("name") or "").strip()
            if preferred_name:
                preferred_device = match_device_by_name(video_devices, preferred_name)
    payload["default_video_device"] = default_device
    payload["default_selection_reason"] = selection_reason if default_device is not None else ""
    payload["preferred_video_device"] = preferred_device
    return payload


def select_camera(args: argparse.Namespace) -> dict[str, Any]:
    video_device, _audio_device, selection_reason, _payload = resolve_devices_for_capture(
        camera_name=args.camera_name,
        video_index=args.video_index,
        with_audio=False,
        audio_index=None,
        command_label="select-camera",
    )
    save_preferred_camera(video_device, selection_reason=selection_reason)
    return {
        "ok": True,
        "action": "select-camera",
        "camera_name": video_device.get("name"),
        "camera_unique_id": video_device.get("unique_id"),
        "video_index": video_device.get("index"),
        "selection_reason": selection_reason,
        "saved_at": iso_now(),
    }


def capture_photo(args: argparse.Namespace) -> dict[str, Any]:
    active_state, is_active = stale_or_active_state()
    if is_active:
        raise CaptureError(
            "A video recording is already running. Stop it before taking a still photo.\n"
            f"Current output: {active_state.get('recording_path') or active_state.get('output_path')}"
        )
    video_device, _audio_device, selection_reason, _payload = resolve_devices_for_capture(
        camera_name=args.camera_name,
        video_index=args.video_index,
        with_audio=False,
        audio_index=None,
        command_label="photo",
    )
    session_name, session_dir, photos_dir, _videos_dir = create_session_dirs(args.session)
    photo_path = photos_dir / f"IMG_{now_stamp()}.jpg"
    helper_args = [
        "photo",
        "--output",
        str(photo_path),
        "--video-unique-id",
        str(video_device.get("unique_id") or ""),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--fps",
        str(args.fps),
    ]
    payload = invoke_native_helper(helper_args, timeout_sec=30)
    save_preferred_camera(video_device, selection_reason=selection_reason)
    return {
        "ok": True,
        "action": "photo",
        "session": session_name,
        "session_dir": str(session_dir.resolve()),
        "photo_path": str(photo_path.resolve()),
        "camera_name": payload.get("camera_name") or video_device.get("name"),
        "camera_unique_id": str(video_device.get("unique_id") or ""),
        "selection_reason": selection_reason,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "captured_at": iso_now(),
    }


def start_video(args: argparse.Namespace) -> dict[str, Any]:
    existing_state, is_active = stale_or_active_state()
    if is_active:
        raise CaptureError(
            "A video recording is already running.\n"
            f"Current output: {existing_state.get('recording_path') or existing_state.get('output_path')}"
        )
    binary = ensure_native_helper()
    video_device, audio_device, selection_reason, _payload = resolve_devices_for_capture(
        camera_name=args.camera_name,
        video_index=args.video_index,
        with_audio=args.with_audio,
        audio_index=args.audio_index,
        command_label="start-video",
    )
    session_name, session_dir, _photos_dir, videos_dir = create_session_dirs(args.session)
    stamp = now_stamp()
    raw_output_path = videos_dir / f"VID_{stamp}.mov"
    final_output_path = videos_dir / f"VID_{stamp}.mp4"
    log_path = videos_dir / f"VID_{stamp}.native.log"
    ready_path = videos_dir / f"VID_{stamp}.ready.json"
    cmd = [
        str(binary),
        "record",
        "--output",
        str(raw_output_path),
        "--ready-path",
        str(ready_path),
        "--video-unique-id",
        str(video_device.get("unique_id") or ""),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--fps",
        str(args.fps),
    ]
    if args.with_audio and audio_device is not None:
        cmd.extend(["--with-audio", "true", "--audio-unique-id", str(audio_device.get("unique_id") or "")])
    process = spawn_native_record(cmd, log_path)
    ready_payload = wait_for_ready(process, ready_path, log_path, timeout_sec=12.0)
    save_preferred_camera(video_device, selection_reason=selection_reason)
    state = {
        "pid": process.pid,
        "pgid": process.pid,
        "session": session_name,
        "session_dir": str(session_dir.resolve()),
        "recording_path": str(raw_output_path.resolve()),
        "output_path": str(final_output_path.resolve()),
        "log_path": str(log_path.resolve()),
        "ready_path": str(ready_path.resolve()),
        "camera_name": video_device.get("name"),
        "camera_unique_id": str(video_device.get("unique_id") or ""),
        "with_audio": bool(args.with_audio),
        "audio_name": audio_device.get("name") if audio_device is not None else "",
        "audio_unique_id": str(audio_device.get("unique_id") or "") if audio_device is not None else "",
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "started_at": iso_now(),
        "selection_reason": selection_reason,
        "command": cmd,
    }
    write_state(state)
    return {
        "ok": True,
        "action": "start-video",
        **state,
        "native_ready": ready_payload,
    }


def stop_video(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise CaptureError("No active recording state file was found.")
    pid = int(state.get("pid", 0) or 0)
    if pid <= 0 or not process_is_alive(pid):
        clear_state(save_last=True)
        raise CaptureError("Recording state exists, but the recorder process is no longer running.")
    stop_process_group(pid, int(state.get("pgid", pid)), timeout_sec=float(args.timeout_sec))
    finished_state = clear_state(save_last=True) or state
    raw_output_path = Path(str(finished_state.get("recording_path") or "")).resolve()
    final_output_path = Path(str(finished_state.get("output_path") or "")).resolve()
    selected_output = raw_output_path
    remux_note = ""
    if raw_output_path.exists():
        selected_output, remux_note = remux_mov_to_mp4(raw_output_path, final_output_path)
    log_path_str = str(finished_state.get("log_path") or "").strip()
    result: dict[str, Any] = {
        "ok": True,
        "action": "stop-video",
        "session": finished_state.get("session"),
        "session_dir": finished_state.get("session_dir"),
        "recording_path": str(raw_output_path) if raw_output_path else "",
        "output_path": str(selected_output) if selected_output else "",
        "log_path": str(Path(log_path_str).resolve()) if log_path_str else "",
        "camera_name": finished_state.get("camera_name"),
        "camera_unique_id": finished_state.get("camera_unique_id"),
        "stopped_at": iso_now(),
        "started_at": finished_state.get("started_at"),
        "with_audio": finished_state.get("with_audio", False),
    }
    if remux_note:
        result["remux_note"] = remux_note
    if selected_output and selected_output.exists():
        result["file_size_bytes"] = selected_output.stat().st_size
    if log_path_str:
        log_path = Path(log_path_str)
        if log_path.exists():
            result["log_tail"] = tail_text(log_path, max_lines=20)
    return result


def status() -> dict[str, Any]:
    state, is_active = stale_or_active_state()
    if not state:
        return {"ok": True, "active": False}
    result = {
        "ok": True,
        "active": bool(is_active),
        "session": state.get("session"),
        "session_dir": state.get("session_dir"),
        "recording_path": state.get("recording_path"),
        "output_path": state.get("output_path"),
        "log_path": state.get("log_path"),
        "camera_name": state.get("camera_name"),
        "camera_unique_id": state.get("camera_unique_id"),
        "started_at": state.get("started_at"),
        "with_audio": state.get("with_audio", False),
        "width": state.get("width"),
        "height": state.get("height"),
        "fps": state.get("fps"),
    }
    if is_active:
        result["pid"] = state.get("pid")
    else:
        result["stale"] = True
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control a macOS camera using a native AVFoundation helper."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_selection_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--camera-name", default="", help="Optional camera name override.")
        subparser.add_argument("--video-index", type=int, default=None, help="Override video device index.")

    def add_common_camera_args(subparser: argparse.ArgumentParser) -> None:
        add_selection_args(subparser)
        subparser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="Capture width.")
        subparser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="Capture height.")
        subparser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="Capture frame rate.")
        subparser.add_argument("--session", default="", help="Session subfolder name under workspace/captures.")
        subparser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    list_parser = subparsers.add_parser("list", help="List native camera and audio devices.")
    list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    select_parser = subparsers.add_parser("select-camera", help="Choose and save the default camera for future commands.")
    add_selection_args(select_parser)
    select_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    photo_parser = subparsers.add_parser("photo", help="Capture one still photo.")
    add_common_camera_args(photo_parser)

    start_parser = subparsers.add_parser("start-video", help="Start background video recording.")
    add_common_camera_args(start_parser)
    start_parser.add_argument("--with-audio", action="store_true", help="Also capture matching audio.")
    start_parser.add_argument("--audio-index", type=int, default=None, help="Override audio device index.")

    stop_parser = subparsers.add_parser("stop-video", help="Stop the current background recording.")
    stop_parser.add_argument("--timeout-sec", type=float, default=20.0, help="Graceful stop timeout.")
    stop_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    status_parser = subparsers.add_parser("status", help="Show current recording status.")
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    return parser


def emit(result: dict[str, Any], json_output: bool) -> int:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    command = str(result.get("action") or "")
    if command == "select-camera":
        print(f"Default camera selected: {result['camera_name']}")
        print(f"Unique ID: {result['camera_unique_id']}")
        return 0
    if command == "photo":
        print(f"Photo saved: {result['photo_path']}")
        print(f"Camera: {result['camera_name']}")
        print(f"Session dir: {result['session_dir']}")
        return 0
    if command == "start-video":
        print(f"Recording started: {result['recording_path']}")
        print(f"Final MP4 target: {result['output_path']}")
        print(f"Camera: {result['camera_name']}")
        print(f"PID: {result['pid']}")
        print(f"Session dir: {result['session_dir']}")
        return 0
    if command == "stop-video":
        print(f"Recording stopped: {result['output_path']}")
        if result.get("camera_name"):
            print(f"Camera: {result['camera_name']}")
        if result.get("file_size_bytes") is not None:
            print(f"File size: {result['file_size_bytes']} bytes")
        print(f"Session dir: {result['session_dir']}")
        return 0
    if "video_devices" in result:
        default_unique_id = str((result.get("default_video_device") or {}).get("unique_id") or "")
        preferred_unique_id = str((result.get("preferred_video_device") or {}).get("unique_id") or "")
        print("Video devices:")
        for item in result["video_devices"]:
            tags: list[str] = []
            item_unique_id = str(item.get("unique_id") or "")
            if item_unique_id and item_unique_id == default_unique_id:
                tags.append("default")
            if item_unique_id and item_unique_id == preferred_unique_id:
                tags.append("saved")
            suffix = f" [{' / '.join(tags)}]" if tags else ""
            print(f"  [{item['index']}] {item['name']} ({item['unique_id']}){suffix}")
        print("Audio devices:")
        for item in result["audio_devices"]:
            print(f"  [{item['index']}] {item['name']} ({item['unique_id']})")
        reason = str(result.get("default_selection_reason") or "").strip()
        if reason and result.get("default_video_device"):
            print(f"Auto default reason: {reason}")
        return 0
    if result.get("active"):
        print(f"Recording active: {result['recording_path']}")
        print(f"Final MP4 target: {result['output_path']}")
        if result.get("camera_name"):
            print(f"Camera: {result['camera_name']}")
        print(f"PID: {result['pid']}")
        return 0
    print("No active recording.")
    return 0


def main() -> int:
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "list":
            result = list_devices()
        elif args.command == "select-camera":
            result = select_camera(args)
        elif args.command == "photo":
            result = capture_photo(args)
        elif args.command == "start-video":
            result = start_video(args)
        elif args.command == "stop-video":
            result = stop_video(args)
        elif args.command == "status":
            result = status()
        else:
            raise CaptureError(f"Unsupported command: {args.command}")
        return emit(result, getattr(args, "json", False))
    except CaptureError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
