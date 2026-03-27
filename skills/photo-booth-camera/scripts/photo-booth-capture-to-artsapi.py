#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = SKILL_ROOT / "workspace" / "runtime" / "generated"
PHOTO_BOOTH_TAKE_PHOTO = SKILL_ROOT / "scripts" / "photo-booth-take-photo.sh"
PHOTO_BOOTH_LATEST_PHOTO = SKILL_ROOT / "scripts" / "photo-booth-latest-photo.sh"
ARTSAPI_CLI = SKILL_ROOT.parent / "artsapi-image-video" / "scripts" / "artsapi_cli.py"
CAPTURE_PREFIX = "Photo captured: "


class CliError(Exception):
    pass


def parse_json_maybe(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def require_path(path: Path, label: str) -> None:
    if not path.exists():
        raise CliError(f"{label} not found: {path}")


def add_photo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--photo-launch-delay",
        type=float,
        default=2,
        help="Wait after opening Photo Booth. Default: 2",
    )
    parser.add_argument(
        "--photo-before-shot-delay",
        type=float,
        default=2,
        help="Wait before pressing the shutter. Default: 2",
    )
    parser.add_argument(
        "--photo-after-shot-delay",
        type=float,
        default=1,
        help="Wait after capture before quitting. Default: 1",
    )
    parser.add_argument(
        "--photo-save-timeout",
        type=int,
        default=15,
        help="How long to wait for the saved file. Default: 15",
    )
    parser.add_argument(
        "--photo-quit-after",
        action="store_true",
        help="Quit Photo Booth after a verified capture.",
    )
    parser.add_argument(
        "--photo-reveal",
        action="store_true",
        help="Reveal the captured photo in Finder.",
    )
    parser.add_argument(
        "--photo-no-countdown",
        action="store_true",
        help="Hold Option while clicking the shutter.",
    )
    parser.add_argument(
        "--photo-no-flash",
        action="store_true",
        help="Hold Shift while clicking the shutter.",
    )
    parser.add_argument(
        "--photo-wait-only",
        action="store_true",
        help="Open Photo Booth and wait for a manual shutter press.",
    )


def add_artsapi_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artsapi-config", help="Override ArtsAPI config file path.")
    parser.add_argument("--artsapi-api-key", help="Override ArtsAPI API key.")
    parser.add_argument("--artsapi-base-url", help="Override ArtsAPI base URL.")
    parser.add_argument(
        "--artsapi-timeout",
        type=int,
        help="Override ArtsAPI HTTP timeout in seconds.",
    )
    parser.add_argument("--model", help="Override model name.")
    parser.add_argument("--negative-prompt", help="Negative prompt.")
    parser.add_argument(
        "--extra-json",
        help='Extra provider-native JSON object, for example: \'{"fps":30}\'',
    )


def build_capture_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(PHOTO_BOOTH_TAKE_PHOTO),
        "--launch-delay",
        str(args.photo_launch_delay),
        "--before-shot-delay",
        str(args.photo_before_shot_delay),
        "--after-shot-delay",
        str(args.photo_after_shot_delay),
        "--save-timeout",
        str(args.photo_save_timeout),
    ]
    if args.photo_quit_after:
        cmd.append("--quit-after")
    if args.photo_reveal:
        cmd.append("--reveal")
    if args.photo_no_countdown:
        cmd.append("--no-countdown")
    if args.photo_no_flash:
        cmd.append("--no-flash")
    if args.photo_wait_only:
        cmd.append("--wait-only")
    return cmd


def parse_capture_path(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(CAPTURE_PREFIX):
            return line[len(CAPTURE_PREFIX) :].strip()
    return None


def fallback_latest_photo() -> str | None:
    result = subprocess.run(
        [str(PHOTO_BOOTH_LATEST_PHOTO)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    latest = result.stdout.strip()
    return latest or None


def run_capture(args: argparse.Namespace) -> str:
    require_path(PHOTO_BOOTH_TAKE_PHOTO, "Photo Booth capture script")
    require_path(PHOTO_BOOTH_LATEST_PHOTO, "Photo Booth latest-photo script")

    result = subprocess.run(
        build_capture_command(args),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CliError(
            "Photo Booth capture failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    capture_path = parse_capture_path(result.stdout) or fallback_latest_photo()
    if not capture_path:
        raise CliError(
            "Photo Booth capture succeeded but no captured file path could be determined.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    path_obj = Path(capture_path).expanduser()
    if not path_obj.exists():
        raise CliError(f"Captured file was reported but does not exist: {path_obj}")
    return str(path_obj)


def build_artsapi_command(args: argparse.Namespace, capture_path: str) -> list[str]:
    require_path(ARTSAPI_CLI, "ArtsAPI CLI script")

    cmd = [sys.executable, str(ARTSAPI_CLI)]
    if args.artsapi_config:
        cmd.extend(["--config", args.artsapi_config])
    if args.artsapi_api_key:
        cmd.extend(["--api-key", args.artsapi_api_key])
    if args.artsapi_base_url:
        cmd.extend(["--base-url", args.artsapi_base_url])
    if args.artsapi_timeout is not None:
        cmd.extend(["--timeout", str(args.artsapi_timeout)])

    cmd.append(args.mode)
    cmd.extend(["--prompt", args.prompt, "--image-url", capture_path])
    cmd.extend(["--save-local", "--save-dir", str(GENERATED_DIR)])

    if args.model:
        cmd.extend(["--model", args.model])
    if args.negative_prompt:
        cmd.extend(["--negative-prompt", args.negative_prompt])
    if args.extra_json:
        cmd.extend(["--extra-json", args.extra_json])

    if args.mode == "image":
        if args.n is not None:
            cmd.extend(["--n", str(args.n)])
        if args.size:
            cmd.extend(["--size", args.size])
        if args.seed is not None:
            cmd.extend(["--seed", str(args.seed)])
        if args.response_format:
            cmd.extend(["--response-format", args.response_format])
        if args.watermark:
            cmd.append("--watermark")
    elif args.mode == "video":
        if args.duration is not None:
            cmd.extend(["--duration", str(args.duration)])
        if args.resolution:
            cmd.extend(["--resolution", args.resolution])
        if args.ratio:
            cmd.extend(["--ratio", args.ratio])
        if args.no_poll:
            cmd.append("--no-poll")
        if args.poll_interval is not None:
            cmd.extend(["--poll-interval", str(args.poll_interval)])
        if args.max_wait is not None:
            cmd.extend(["--max-wait", str(args.max_wait)])

    return cmd


def run_artsapi(args: argparse.Namespace, capture_path: str) -> tuple[int, Any, str]:
    result = subprocess.run(
        build_artsapi_command(args, capture_path),
        capture_output=True,
        text=True,
        check=False,
    )
    parsed = parse_json_maybe(result.stdout)
    stderr_text = result.stderr.strip()
    return result.returncode, parsed, stderr_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a Photo Booth photo, then send it to ArtsAPI."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    image_parser = subparsers.add_parser(
        "image",
        help="Take a photo, then use it as ArtsAPI image-to-image input.",
    )
    add_photo_args(image_parser)
    add_artsapi_args(image_parser)
    image_parser.add_argument("--prompt", required=True, help="ArtsAPI prompt text.")
    image_parser.add_argument("--n", type=int, help="Number of images.")
    image_parser.add_argument("--size", help="Image size, for example 1024x1024.")
    image_parser.add_argument("--seed", type=int, help="Seed value.")
    image_parser.add_argument(
        "--response-format",
        choices=["url", "b64_json"],
        help="Response format.",
    )
    image_parser.add_argument("--watermark", action="store_true", help="Enable watermark.")

    video_parser = subparsers.add_parser(
        "video",
        help="Take a photo, then use it as ArtsAPI image-to-video input.",
    )
    add_photo_args(video_parser)
    add_artsapi_args(video_parser)
    video_parser.add_argument("--prompt", required=True, help="ArtsAPI prompt text.")
    video_parser.add_argument("--duration", type=int, help="Duration in seconds.")
    video_parser.add_argument("--resolution", help="Resolution, for example 720p.")
    video_parser.add_argument("--ratio", help="Aspect ratio, for example 16:9.")
    video_parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Only submit the video task without polling.",
    )
    video_parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Polling interval in seconds. Default: 5",
    )
    video_parser.add_argument(
        "--max-wait",
        type=int,
        default=900,
        help="Maximum polling time in seconds. Default: 900",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        capture_path = run_capture(args)
        return_code, artsapi_response, artsapi_stderr = run_artsapi(args, capture_path)
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

    payload: dict[str, Any] = {
        "capture_path": capture_path,
        "mode": args.mode,
        "prompt": args.prompt,
        "artsapi": artsapi_response,
    }
    if artsapi_stderr:
        payload["artsapi_stderr"] = artsapi_stderr

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
