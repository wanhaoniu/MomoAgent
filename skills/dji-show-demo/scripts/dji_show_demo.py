#!/usr/bin/env python3
"""Unified helper for the dji-show-demo OpenClaw skill."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = SKILL_ROOT / "workspace" / "runtime"
GENERATED_DIR = RUNTIME_DIR / "generated"
STATE_PATH = RUNTIME_DIR / "dji_show_demo_state.json"
DEFAULT_SOARMMOCE_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "soarmmoce-real-con"
DEFAULT_PHOTO_BOOTH_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "photo-booth-camera"
DEFAULT_MACOS_USE_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "macos-use-desktop-control"
DEFAULT_ARTSAPI_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "artsapi-image-video"
DEFAULT_PHOTO_BOOTH_LIBRARY_DIR = Path.home() / "Pictures" / "Photo Booth Library"
DEFAULT_SHARE_MEDIA_DIR = Path.home() / ".openclaw" / "media" / "outbound"
DEFAULT_STEP_M = 0.01
DEFAULT_PHOTO_SWEEP_DEG = 35.0
DEFAULT_TRAJECTORY_SWEEP_DEG = 48.0
DEFAULT_POSTER_PROMPT = "把这张照片改成电影海报风格，保留人物主体，增强光影、层次和质感，适合展示海报。"
DEFAULT_VIDEO_START_SETTLE_S = 1.0
DEFAULT_VIDEO_SAVE_TIMEOUT_S = 30.0

AXIS_BY_DIRECTION = {
    "left": "dy",
    "right": "dy",
    "forward": "dx",
    "backward": "dx",
    "back": "dx",
    "up": "dz",
    "down": "dz",
}

ENV_KEY_BY_DIRECTION = {
    "left": "DJI_SHOW_DEMO_LEFT_DELTA_M",
    "right": "DJI_SHOW_DEMO_RIGHT_DELTA_M",
    "forward": "DJI_SHOW_DEMO_FORWARD_DELTA_M",
    "backward": "DJI_SHOW_DEMO_BACKWARD_DELTA_M",
    "back": "DJI_SHOW_DEMO_BACKWARD_DELTA_M",
    "up": "DJI_SHOW_DEMO_UP_DELTA_M",
    "down": "DJI_SHOW_DEMO_DOWN_DELTA_M",
}

DEFAULT_DIRECTION_M = {
    "left": 0.15,
    "right": -0.15,
    "forward": 0.01,
    "backward": -0.01,
    "back": -0.01,
    "up": 0.01,
    "down": -0.01,
}


def _success_payload(data: Any) -> dict[str, Any]:
    return {"ok": True, "result": data, "error": None}


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "result": None,
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }


def _env_path(key: str, default: Path) -> Path:
    raw = str(os.environ.get(key, "") or "").strip()
    return Path(raw).expanduser() if raw else default


def _env_float(key: str, default: float) -> float:
    raw = str(os.environ.get(key, "") or "").strip()
    return float(raw) if raw else float(default)


def _python_bin() -> str:
    return str(Path(sys.executable or "python3"))


def _soarmmoce_move_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_SOARMMOCE_SKILL_ROOT", DEFAULT_SOARMMOCE_SKILL_ROOT)
    script = root / "scripts" / "soarmmoce_move.py"
    if not script.exists():
        raise FileNotFoundError(f"soarmmoce move script not found: {script}")
    return script


def _photo_booth_take_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_PHOTO_BOOTH_SKILL_ROOT", DEFAULT_PHOTO_BOOTH_SKILL_ROOT)
    script = root / "scripts" / "photo-booth-take-photo.sh"
    if not script.exists():
        raise FileNotFoundError(f"photo booth capture script not found: {script}")
    return script


def _photo_booth_preflight_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_PHOTO_BOOTH_SKILL_ROOT", DEFAULT_PHOTO_BOOTH_SKILL_ROOT)
    script = root / "scripts" / "photo-booth-preflight.sh"
    if not script.exists():
        raise FileNotFoundError(f"photo booth preflight script not found: {script}")
    return script


def _photo_booth_latest_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_PHOTO_BOOTH_SKILL_ROOT", DEFAULT_PHOTO_BOOTH_SKILL_ROOT)
    script = root / "scripts" / "photo-booth-latest-photo.sh"
    if not script.exists():
        raise FileNotFoundError(f"photo booth latest-photo script not found: {script}")
    return script


def _photo_booth_record_script() -> Path:
    script = SKILL_ROOT / "scripts" / "photo-booth-record-video.sh"
    if not script.exists():
        raise FileNotFoundError(f"photo booth record script not found: {script}")
    return script


def _books_demo_script() -> Path:
    script = SKILL_ROOT / "scripts" / "books-demo-control.sh"
    if not script.exists():
        raise FileNotFoundError(f"books demo control script not found: {script}")
    return script


def _macos_use_control_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_MACOS_USE_SKILL_ROOT", DEFAULT_MACOS_USE_SKILL_ROOT)
    script = root / "scripts" / "macos_use_control.py"
    if not script.exists():
        raise FileNotFoundError(f"macos-use control script not found: {script}")
    return script


def _artsapi_cli_script() -> Path:
    root = _env_path("DJI_SHOW_DEMO_ARTSAPI_SKILL_ROOT", DEFAULT_ARTSAPI_SKILL_ROOT)
    script = root / "scripts" / "artsapi_cli.py"
    if not script.exists():
        raise FileNotFoundError(f"ArtsAPI CLI script not found: {script}")
    return script


def _share_media_dir() -> Path:
    return _env_path("DJI_SHOW_DEMO_SHARE_MEDIA_DIR", DEFAULT_SHARE_MEDIA_DIR)


def _run_command(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, env=env)


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON output, got: {stdout.strip() or '<empty>'}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected JSON object output")
    return payload


def _run_soarmmoce_move(*args: str) -> dict[str, Any]:
    command = [_python_bin(), str(_soarmmoce_move_script()), *args]
    proc = _run_command(command)
    payload = _parse_json_stdout(proc.stdout)
    if proc.returncode != 0 or not bool(payload.get("ok")):
        error = payload.get("error") or {}
        message = error.get("message") or proc.stderr.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(str(message))
    return {
        "command": command,
        "stdout": payload,
    }


def _run_soarmmoce_delta(*, dx: float, dy: float, dz: float, duration: float, frame: str = "user") -> dict[str, Any]:
    return _run_soarmmoce_move(
        "delta",
        "--dx",
        str(float(dx)),
        "--dy",
        str(float(dy)),
        "--dz",
        str(float(dz)),
        "--frame",
        frame,
        "--duration",
        str(float(duration)),
    )


def _run_photo_booth(*args: str) -> dict[str, Any]:
    command = ["bash", str(_photo_booth_take_script()), *args]
    env = os.environ.copy()
    env.setdefault("PHOTO_BOOTH_USE_TERMINAL_RUNNER", "1")
    proc = _run_command(command, env=env)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    capture_path = None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Photo captured: "):
            capture_path = stripped[len("Photo captured: ") :].strip()
            break
    return {
        "command": command,
        "env_overrides": {"PHOTO_BOOTH_USE_TERMINAL_RUNNER": env["PHOTO_BOOTH_USE_TERMINAL_RUNNER"]},
        "stdout": proc.stdout.strip(),
        "capture_path": capture_path,
    }


def _run_photo_booth_preflight() -> dict[str, Any]:
    command = ["bash", str(_photo_booth_preflight_script())]
    proc = _run_command(command)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    return {
        "command": command,
        "stdout": proc.stdout.strip(),
    }


def _run_photo_booth_latest(*args: str) -> dict[str, Any]:
    command = ["bash", str(_photo_booth_latest_script()), *args]
    proc = _run_command(command)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    latest_path = proc.stdout.strip()
    return {
        "command": command,
        "stdout": proc.stdout.strip(),
        "capture_path": latest_path or None,
    }


def _run_photo_booth_video(*args: str) -> dict[str, Any]:
    command = ["bash", str(_photo_booth_record_script()), *args]
    env = os.environ.copy()
    macos_use_script = str(_macos_use_control_script())
    env.setdefault("PHOTO_BOOTH_MACOS_USE_CONTROL_SCRIPT", macos_use_script)
    env.setdefault("DJI_SHOW_DEMO_MACOS_USE_CONTROL_SCRIPT", macos_use_script)
    env.setdefault(
        "DJI_SHOW_DEMO_PHOTO_BOOTH_LIBRARY_DIR",
        str(_env_path("DJI_SHOW_DEMO_PHOTO_BOOTH_LIBRARY_DIR", DEFAULT_PHOTO_BOOTH_LIBRARY_DIR)),
    )
    proc = _run_command(command, env=env)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    return {
        "command": command,
        "stdout": proc.stdout.strip(),
    }


def _run_books_demo(*args: str) -> dict[str, Any]:
    command = ["bash", str(_books_demo_script()), *args]
    proc = _run_command(command)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(message)
    return {
        "command": command,
        "stdout": proc.stdout.strip(),
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(payload: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_state(**updates: Any) -> dict[str, Any]:
    state = _load_state()
    state.update(updates)
    _save_state(state)
    return state


def _open_path(target_path: str) -> None:
    subprocess.run(["open", target_path], check=False)


def _stage_media_for_sharing(source_path: str | None, *, category: str) -> str | None:
    raw = str(source_path or "").strip()
    if not raw:
        return None
    source = Path(raw).expanduser()
    if not source.exists():
        return str(source)
    try:
        share_dir = _share_media_dir() / category
        share_dir.mkdir(parents=True, exist_ok=True)
        target = share_dir / source.name
        try:
            if source.resolve() == target.resolve():
                return str(target)
        except FileNotFoundError:
            pass
        shutil.copy2(source, target)
        return str(target)
    except Exception:
        return str(source)


def _prefixed_output_value(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _start_photo_booth_video_recording(*, settle_s: float) -> dict[str, Any]:
    record_result = _run_photo_booth_video("start", "--record-start-settle-s", str(float(settle_s)))
    baseline_count = _prefixed_output_value(record_result["stdout"], "BASELINE_RECENT_COUNT\t")
    baseline_name = _prefixed_output_value(record_result["stdout"], "BASELINE_RECENT_NAME\t")
    record_button_bounds = _prefixed_output_value(record_result["stdout"], "RECORD_BUTTON_BOUNDS\t")
    return {
        "baseline_recent_count": int(baseline_count) if baseline_count else 0,
        "baseline_recent_name": baseline_name,
        "record_button_bounds": record_button_bounds or None,
        "driver": "terminal_runner",
        "command": record_result["command"],
    }


def _stop_photo_booth_video_recording(
    *,
    baseline_recent_count: int,
    baseline_recent_name: str,
    record_button_bounds: str | None,
    save_timeout_s: float,
    reveal: bool,
) -> dict[str, Any]:
    command_args = ["stop", "--video-save-timeout", str(float(save_timeout_s))]
    baseline_name = str(baseline_recent_name or "").strip()
    if baseline_name:
        command_args.extend(["--baseline-recent-name", baseline_name])
    if int(baseline_recent_count or 0) > 0:
        command_args.extend(["--baseline-recent-count", str(int(baseline_recent_count))])
    button_bounds = str(record_button_bounds or "").strip()
    if button_bounds:
        command_args.extend(["--button-bounds", button_bounds])
    if reveal:
        command_args.append("--reveal")
    record_result = _run_photo_booth_video(*command_args)
    item_path = _prefixed_output_value(record_result["stdout"], "VIDEO_PATH\t")
    if not item_path:
        raise RuntimeError("Video recorder did not return a saved video path")
    shared_item_path = _stage_media_for_sharing(item_path, category="videos")
    return {
        "saved_item_name": Path(shared_item_path or item_path).name if (shared_item_path or item_path) else None,
        "saved_item_original_path": item_path or None,
        "saved_item_path": shared_item_path or item_path or None,
        "driver": "terminal_runner",
        "command": record_result["command"],
    }


def _latest_saved_photo_path() -> str:
    state = _load_state()
    state_path = str(state.get("last_photo_path") or "").strip()
    if state_path and Path(state_path).expanduser().exists():
        return str(Path(state_path).expanduser())
    latest = _run_photo_booth_latest()
    latest_path = str(latest.get("capture_path") or "").strip()
    if latest_path and Path(latest_path).expanduser().exists():
        return str(Path(latest_path).expanduser())
    raise RuntimeError("No saved Photo Booth photo is available for poster generation")


def _parse_json_or_raise(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON output, got: {stdout.strip() or '<empty>'}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected JSON object output")
    return payload


def _run_artsapi_image(
    *,
    image_path: str,
    prompt: str,
    model: str | None,
    size: str | None,
    negative_prompt: str | None,
    timeout: float,
) -> dict[str, Any]:
    command = [
        _python_bin(),
        str(_artsapi_cli_script()),
        "--timeout",
        str(int(timeout)),
        "image",
        "--prompt",
        prompt,
        "--image-url",
        image_path,
        "--save-local",
        "--save-dir",
        str(GENERATED_DIR),
    ]
    if model:
        command.extend(["--model", model])
    if size:
        command.extend(["--size", size])
    if negative_prompt:
        command.extend(["--negative-prompt", negative_prompt])

    proc = _run_command(command)
    payload = _parse_json_or_raise(proc.stdout)
    if proc.returncode != 0:
        message = payload.get("msg") or proc.stderr.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(str(message))
    return {
        "command": command,
        "stdout": payload,
    }


def _direction_delta_m(direction: str, explicit_step_m: float | None) -> float:
    key = str(direction).strip().lower()
    if explicit_step_m is not None:
        base = abs(float(explicit_step_m))
        default_sign = 1.0 if float(DEFAULT_DIRECTION_M[key]) >= 0.0 else -1.0
        return default_sign * base
    env_key = ENV_KEY_BY_DIRECTION[key]
    return _env_float(env_key, DEFAULT_DIRECTION_M[key])


def _direction_delta_kwargs(direction: str, explicit_step_m: float | None) -> dict[str, float]:
    normalized = str(direction).strip().lower()
    axis = AXIS_BY_DIRECTION[normalized]
    delta_value = _direction_delta_m(normalized, explicit_step_m)
    return {
        "dx": float(delta_value) if axis == "dx" else 0.0,
        "dy": float(delta_value) if axis == "dy" else 0.0,
        "dz": float(delta_value) if axis == "dz" else 0.0,
    }


def _move_direction(direction: str, *, step_m: float | None, duration: float) -> dict[str, Any]:
    normalized = str(direction).strip().lower()
    if normalized not in AXIS_BY_DIRECTION:
        raise ValueError(f"Unsupported direction: {direction}")
    delta_kwargs = _direction_delta_kwargs(normalized, step_m)
    motion = _run_soarmmoce_delta(
        dx=delta_kwargs["dx"],
        dy=delta_kwargs["dy"],
        dz=delta_kwargs["dz"],
        duration=float(duration),
        frame="user",
    )
    return {
        "action": "move_direction",
        "direction": normalized,
        "frame": "user",
        "delta_m": delta_kwargs,
        "motion": motion["stdout"]["result"],
    }


def _home(*, duration: float) -> dict[str, Any]:
    motion = _run_soarmmoce_move("home", "--duration", str(float(duration)))
    return {
        "action": "home",
        "motion": motion["stdout"]["result"],
    }


def _run_trajectory_motion(
    *,
    sweep_deg: float,
    to_left_duration: float,
    sweep_duration: float,
    home_duration: float,
    skip_pre_home: bool,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    sweep_deg = abs(float(sweep_deg))

    if not bool(skip_pre_home):
        pre_home_result = _run_soarmmoce_move("home", "--duration", str(float(home_duration)))
        steps.append({"step": "trajectory_pre_home", "motion": pre_home_result["stdout"]["result"]})

    left_result = _run_soarmmoce_move(
        "joint",
        "--joint",
        "shoulder_pan",
        "--delta-deg",
        str(sweep_deg),
        "--duration",
        str(float(to_left_duration)),
    )
    steps.append(
        {
            "step": "trajectory_to_left",
            "delta_deg": sweep_deg,
            "motion": left_result["stdout"]["result"],
        }
    )

    right_result = _run_soarmmoce_move(
        "joint",
        "--joint",
        "shoulder_pan",
        "--delta-deg",
        str(-2.0 * sweep_deg),
        "--duration",
        str(float(sweep_duration)),
    )
    steps.append(
        {
            "step": "trajectory_left_to_right",
            "delta_deg": float(-2.0 * sweep_deg),
            "motion": right_result["stdout"]["result"],
        }
    )

    return steps


def _trajectory_sequence(args: argparse.Namespace) -> dict[str, Any]:
    steps = _run_trajectory_motion(
        sweep_deg=float(args.sweep_deg),
        to_left_duration=float(args.to_left_duration),
        sweep_duration=float(args.sweep_duration),
        home_duration=float(args.home_duration),
        skip_pre_home=bool(args.skip_pre_home),
    )
    _merge_state(last_action="trajectory")
    return {
        "action": "trajectory_sequence",
        "joint": "shoulder_pan",
        "steps": steps,
    }


def _trajectory_demo_sequence(args: argparse.Namespace) -> dict[str, Any]:
    steps = _run_trajectory_motion(
        sweep_deg=float(args.sweep_deg),
        to_left_duration=float(args.to_left_duration),
        sweep_duration=float(args.sweep_duration),
        home_duration=float(args.home_duration),
        skip_pre_home=bool(args.skip_pre_home),
    )
    home_result = _home(duration=float(args.home_duration))
    steps.append({"step": "trajectory_demo_home", "motion": home_result["motion"]})
    _merge_state(last_action="trajectory_demo")
    return {
        "action": "trajectory_demo_sequence",
        "joint": "shoulder_pan",
        "steps": steps,
    }


def _trajectory_record_sequence(args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    recording = _start_photo_booth_video_recording(
        settle_s=float(
            getattr(args, "record_start_settle_s", _env_float("DJI_SHOW_DEMO_VIDEO_START_SETTLE_S", DEFAULT_VIDEO_START_SETTLE_S))
        )
    )
    steps.append(
        {
            "step": "video_record_start",
            "recording": recording,
        }
    )

    motion_error: str | None = None
    try:
        motion_steps = _run_trajectory_motion(
            sweep_deg=float(args.sweep_deg),
            to_left_duration=float(args.to_left_duration),
            sweep_duration=float(args.sweep_duration),
            home_duration=float(args.home_duration),
            skip_pre_home=bool(args.skip_pre_home),
        )
        steps.extend(motion_steps)
        home_result = _home(duration=float(args.home_duration))
        steps.append({"step": "trajectory_record_home", "motion": home_result["motion"]})
    except Exception as exc:
        motion_error = str(exc)
        raise
    finally:
        try:
            stop_result = _stop_photo_booth_video_recording(
                baseline_recent_count=int(recording.get("baseline_recent_count") or 0),
                baseline_recent_name=str(recording.get("baseline_recent_name") or ""),
                record_button_bounds=str(recording.get("record_button_bounds") or ""),
                save_timeout_s=float(args.video_save_timeout),
                reveal=bool(args.reveal),
            )
            steps.append({"step": "video_record_stop", "recording": stop_result})
            video_path = str(stop_result.get("saved_item_path") or "").strip() or None
            _merge_state(
                last_action="trajectory_record",
                last_video_path=video_path,
                last_video_motion_error=motion_error,
            )
        except Exception as stop_exc:
            _merge_state(
                last_action="trajectory_record",
                last_video_motion_error=motion_error or str(stop_exc),
            )
            if motion_error:
                raise RuntimeError(f"{motion_error}; additionally failed to stop recording: {stop_exc}") from stop_exc
            raise

    final_video = steps[-1]["recording"]
    return {
        "action": "trajectory_record_sequence",
        "joint": "shoulder_pan",
        "steps": steps,
        "video_path": final_video.get("saved_item_path"),
    }


def _books_open_recent_sequence(args: argparse.Namespace) -> dict[str, Any]:
    command_args = ["open-recent"]
    if bool(args.dry_run):
        command_args.append("--dry-run")
    if bool(args.skip_activate):
        command_args.append("--skip-activate")
    if bool(args.skip_move):
        command_args.append("--skip-move")
    if args.open_delay_ms is not None:
        command_args.extend(["--open-delay-ms", str(float(args.open_delay_ms))])

    books_result = _run_books_demo(*command_args)
    _merge_state(last_action="books_open_recent")
    return {
        "action": "books_open_recent",
        "books": books_result,
    }


def _books_next_page_sequence(args: argparse.Namespace) -> dict[str, Any]:
    command_args = ["next-page-key" if bool(args.use_key) else "next-page"]
    if bool(args.dry_run):
        command_args.append("--dry-run")
    if bool(args.skip_activate):
        command_args.append("--skip-activate")

    books_result = _run_books_demo(*command_args)
    _merge_state(last_action="books_next_page")
    return {
        "action": "books_next_page",
        "books": books_result,
        "driver": "key" if bool(args.use_key) else "click",
    }


def _books_previous_page_sequence(args: argparse.Namespace) -> dict[str, Any]:
    command_args = ["previous-page-key"]
    if bool(args.dry_run):
        command_args.append("--dry-run")
    if bool(args.skip_activate):
        command_args.append("--skip-activate")

    books_result = _run_books_demo(*command_args)
    _merge_state(last_action="books_previous_page")
    return {
        "action": "books_previous_page",
        "books": books_result,
        "driver": "key",
    }


def _photo_sequence(args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    preflight_result = None
    if args.preflight:
        preflight_result = _run_photo_booth_preflight()
        steps.append({"step": "photo_preflight", **preflight_result})

    if not getattr(args, "skip_motion", False):
        sweep_deg = float(args.sweep_deg)
        motion_started = False
        try:
            left_demo = _run_soarmmoce_move(
                "joint",
                "--joint",
                "shoulder_pan",
                "--delta-deg",
                str(sweep_deg),
                "--duration",
                str(float(args.move_duration)),
            )
            motion_started = True
            steps.append({"step": "demo_pan_first", "delta_deg": sweep_deg, "motion": left_demo["stdout"]["result"]})

            right_demo = _run_soarmmoce_move(
                "joint",
                "--joint",
                "shoulder_pan",
                "--delta-deg",
                str(-2.0 * sweep_deg),
                "--duration",
                str(float(args.move_duration)),
            )
            steps.append(
                {
                    "step": "demo_pan_second",
                    "delta_deg": float(-2.0 * sweep_deg),
                    "motion": right_demo["stdout"]["result"],
                }
            )

            home_result = _run_soarmmoce_move("home", "--duration", str(float(args.home_duration)))
            steps.append({"step": "demo_home", "motion": home_result["stdout"]["result"]})

        except Exception:
            if motion_started:
                try:
                    recover_home = _run_soarmmoce_move("home", "--duration", str(float(args.home_duration)))
                    steps.append({"step": "recover_home", "motion": recover_home["stdout"]["result"]})
                except Exception as recover_exc:
                    steps.append({"step": "recover_home_failed", "error": str(recover_exc)})
            raise

    photo_args: list[str] = []
    if float(args.before_shot_delay) > 0.0:
        photo_args.extend(["--before-shot-delay", str(float(args.before_shot_delay))])
    if args.reveal:
        photo_args.append("--reveal")
    if args.quit_after:
        photo_args.append("--quit-after")
    if args.no_countdown:
        photo_args.append("--no-countdown")
    if args.no_flash:
        photo_args.append("--no-flash")
    if args.wait_only:
        photo_args.append("--wait-only")
    if args.dry_run:
        photo_args.append("--dry-run")

    photo_result = _run_photo_booth(*photo_args)
    capture_path = photo_result.get("capture_path")
    shared_capture_path = _stage_media_for_sharing(capture_path, category="photos") if capture_path else None
    if shared_capture_path:
        photo_result["capture_original_path"] = capture_path
        photo_result["capture_path"] = shared_capture_path
        capture_path = shared_capture_path
    steps.append({"step": "photo_capture", **photo_result})

    if capture_path:
        _merge_state(
            last_photo_path=capture_path,
            last_action="photo",
        )
        try:
            _open_path(capture_path)
        except Exception:
            pass

    return {
        "action": "photo_sequence",
        "skill_root": str(SKILL_ROOT),
        "preflight": preflight_result,
        "steps": steps,
        "photo_capture_path": capture_path,
    }


def _extract_saved_files(response: dict[str, Any]) -> list[str]:
    artifacts = response.get("_local_artifacts")
    if not isinstance(artifacts, dict):
        return []
    saved_files = artifacts.get("saved_files")
    if not isinstance(saved_files, list):
        return []
    return [str(item).strip() for item in saved_files if str(item).strip()]


def _poster_from_latest_photo(args: argparse.Namespace) -> dict[str, Any]:
    source_photo_path = _latest_saved_photo_path()
    prompt = str(args.prompt or os.environ.get("DJI_SHOW_DEMO_POSTER_PROMPT") or DEFAULT_POSTER_PROMPT).strip()
    artsapi_result = _run_artsapi_image(
        image_path=source_photo_path,
        prompt=prompt,
        model=args.model,
        size=args.size,
        negative_prompt=args.negative_prompt,
        timeout=float(args.timeout),
    )
    saved_files = _extract_saved_files(artsapi_result["stdout"])
    poster_generated_path = saved_files[0] if saved_files else None
    poster_local_path = _stage_media_for_sharing(poster_generated_path, category="posters") if poster_generated_path else None
    reveal_path = poster_local_path or poster_generated_path
    if reveal_path:
        try:
            _open_path(reveal_path)
        except Exception:
            pass
    _merge_state(
        last_photo_path=source_photo_path,
        last_action="poster",
        last_poster_prompt=prompt,
        last_poster_path=poster_local_path or poster_generated_path,
    )
    return {
        "action": "poster_from_latest_photo",
        "source_photo_path": source_photo_path,
        "prompt": prompt,
        "poster_generated_path": poster_generated_path,
        "poster_local_path": poster_local_path or poster_generated_path,
        "poster_reveal_path": reveal_path,
        "artsapi": artsapi_result["stdout"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DJI show demo helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    photo = sub.add_parser("photo", help="Run the demo sweep, return home, then take a Photo Booth photo")
    photo.add_argument("--sweep-deg", type=float, default=_env_float("DJI_SHOW_DEMO_PHOTO_SWEEP_DEG", DEFAULT_PHOTO_SWEEP_DEG))
    photo.add_argument("--move-duration", type=float, default=1.0)
    photo.add_argument("--home-duration", type=float, default=1.5)
    photo.add_argument("--before-shot-delay", type=float, default=0.0)
    photo.add_argument("--reveal", action="store_true")
    photo.add_argument("--quit-after", action="store_true")
    photo.add_argument("--no-countdown", action="store_true")
    photo.add_argument("--no-flash", action="store_true")
    photo.add_argument("--wait-only", action="store_true")
    photo.add_argument("--dry-run", action="store_true")
    photo.add_argument("--preflight", action="store_true")
    photo.add_argument("--skip-motion", action="store_true")

    snap = sub.add_parser("snap", aliases=["capture"], help="Take a photo immediately without demo motion")
    snap.add_argument("--before-shot-delay", type=float, default=0.0)
    snap.add_argument("--reveal", action="store_true")
    snap.add_argument("--quit-after", action="store_true")
    snap.add_argument("--no-countdown", action="store_true")
    snap.add_argument("--no-flash", action="store_true")
    snap.add_argument("--wait-only", action="store_true")
    snap.add_argument("--dry-run", action="store_true")
    snap.add_argument("--preflight", action="store_true")
    snap.set_defaults(skip_motion=True)

    move = sub.add_parser("move", help="Move one demo direction by a small cartesian dx/dy/dz delta")
    move.add_argument("--direction", required=True, choices=["left", "right", "forward", "backward", "back", "up", "down"])
    move.add_argument("--step-m", type=float, default=None)
    move.add_argument("--duration", type=float, default=1.0)

    trajectory = sub.add_parser(
        "trajectory",
        aliases=["sweep", "track", "path"],
        help="Move shoulder_pan to the left side, then sweep smoothly to the right side",
    )
    trajectory.add_argument(
        "--sweep-deg",
        type=float,
        default=_env_float("DJI_SHOW_DEMO_TRAJECTORY_SWEEP_DEG", DEFAULT_TRAJECTORY_SWEEP_DEG),
    )
    trajectory.add_argument("--to-left-duration", type=float, default=1.5)
    trajectory.add_argument("--sweep-duration", type=float, default=4.0)
    trajectory.add_argument("--home-duration", type=float, default=1.5)
    trajectory.add_argument("--skip-pre-home", action="store_true")

    trajectory_demo = sub.add_parser(
        "trajectory-demo",
        aliases=["showcase-motion", "showcase2-motion", "trajectory-home"],
        help="Run the showcase trajectory, then return home",
    )
    trajectory_demo.add_argument(
        "--sweep-deg",
        type=float,
        default=_env_float("DJI_SHOW_DEMO_TRAJECTORY_SWEEP_DEG", DEFAULT_TRAJECTORY_SWEEP_DEG),
    )
    trajectory_demo.add_argument("--to-left-duration", type=float, default=1.5)
    trajectory_demo.add_argument("--sweep-duration", type=float, default=4.0)
    trajectory_demo.add_argument("--home-duration", type=float, default=1.5)
    trajectory_demo.add_argument("--skip-pre-home", action="store_true")

    trajectory_record = sub.add_parser(
        "trajectory-record",
        aliases=["record-trajectory", "showcase-record", "showcase2-record"],
        help="Start Pocket 3 / Photo Booth recording, run the showcase trajectory, return home, then stop recording",
    )
    trajectory_record.add_argument(
        "--sweep-deg",
        type=float,
        default=_env_float("DJI_SHOW_DEMO_TRAJECTORY_SWEEP_DEG", DEFAULT_TRAJECTORY_SWEEP_DEG),
    )
    trajectory_record.add_argument("--to-left-duration", type=float, default=1.5)
    trajectory_record.add_argument("--sweep-duration", type=float, default=4.0)
    trajectory_record.add_argument("--home-duration", type=float, default=1.5)
    trajectory_record.add_argument("--skip-pre-home", action="store_true")
    trajectory_record.add_argument(
        "--record-start-settle-s",
        type=float,
        default=_env_float("DJI_SHOW_DEMO_VIDEO_START_SETTLE_S", DEFAULT_VIDEO_START_SETTLE_S),
    )
    trajectory_record.add_argument(
        "--video-save-timeout",
        type=float,
        default=_env_float("DJI_SHOW_DEMO_VIDEO_SAVE_TIMEOUT_S", DEFAULT_VIDEO_SAVE_TIMEOUT_S),
    )
    trajectory_record.add_argument("--reveal", action="store_true")

    books_open = sub.add_parser(
        "books-open-recent",
        aliases=["open-recent-book", "books-open"],
        help="Open the current recent book in Books on the main screen",
    )
    books_open.add_argument("--dry-run", action="store_true")
    books_open.add_argument("--skip-activate", action="store_true")
    books_open.add_argument("--skip-move", action="store_true")
    books_open.add_argument("--open-delay-ms", type=float, default=None)

    books_next = sub.add_parser(
        "books-next-page",
        aliases=["books-page-forward", "page-forward"],
        help="Turn to the next page in Books on the main screen reader",
    )
    books_next.add_argument("--dry-run", action="store_true")
    books_next.add_argument("--skip-activate", action="store_true")
    books_next.add_argument("--use-key", action="store_true")

    books_previous = sub.add_parser(
        "books-previous-page",
        aliases=["books-page-back", "page-back"],
        help="Turn to the previous page in Books on the main screen reader",
    )
    books_previous.add_argument("--dry-run", action="store_true")
    books_previous.add_argument("--skip-activate", action="store_true")

    poster = sub.add_parser(
        "poster",
        aliases=["retouch", "edit", "pitu"],
        help="Use the latest saved Photo Booth photo to generate a poster via ArtsAPI",
    )
    poster.add_argument("--prompt", default=None)
    poster.add_argument("--model", default=None)
    poster.add_argument("--size", default=None)
    poster.add_argument("--negative-prompt", default=None)
    poster.add_argument("--timeout", type=float, default=120.0)

    home = sub.add_parser("home", aliases=["go-home"], help="Return the arm to home")
    home.add_argument("--duration", type=float, default=1.5)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.cmd in ("photo", "snap", "capture"):
            result = _photo_sequence(args)
        elif args.cmd == "move":
            result = _move_direction(args.direction, step_m=args.step_m, duration=args.duration)
        elif args.cmd in {"trajectory", "sweep", "track", "path"}:
            result = _trajectory_sequence(args)
        elif args.cmd in {"trajectory-demo", "showcase-motion", "showcase2-motion", "trajectory-home"}:
            result = _trajectory_demo_sequence(args)
        elif args.cmd in {"trajectory-record", "record-trajectory", "showcase-record", "showcase2-record"}:
            result = _trajectory_record_sequence(args)
        elif args.cmd in {"books-open-recent", "open-recent-book", "books-open"}:
            result = _books_open_recent_sequence(args)
        elif args.cmd in {"books-next-page", "books-page-forward", "page-forward"}:
            result = _books_next_page_sequence(args)
        elif args.cmd in {"books-previous-page", "books-page-back", "page-back"}:
            result = _books_previous_page_sequence(args)
        elif args.cmd in {"poster", "retouch", "edit", "pitu"}:
            result = _poster_from_latest_photo(args)
        elif args.cmd in {"home", "go-home"}:
            result = _home(duration=args.duration)
        else:
            raise ValueError(f"Unsupported command: {args.cmd}")
        print(json.dumps(_success_payload(result), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
