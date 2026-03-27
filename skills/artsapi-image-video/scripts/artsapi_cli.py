#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = SKILL_ROOT / "config" / "artsapi.env"
DEFAULT_OUTPUT_DIR = SKILL_ROOT / "workspace" / "runtime" / "generated"
DEFAULT_BASE_URL = "https://api.artsapi.com/api"
DEFAULT_IMAGE_MODEL = "doubao-seedream-5-0-260128"
DEFAULT_VIDEO_MODEL = "doubao-seedance-1-5-pro-251215"
MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024


class CliError(Exception):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    return normalized in {
        "",
        "YOUR_ARTSAPI_KEY_HERE",
        "PLEASE_FILL_ME",
    }


def parse_extra_json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"--extra-json must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CliError("--extra-json must decode to a JSON object")
    return value


def file_uri_to_path(value: str) -> Path:
    parsed = urllib.parse.urlparse(value)
    path = urllib.parse.unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return Path(path).expanduser()


def local_image_to_data_url(path: Path, flag_name: str) -> str:
    if not path.exists():
        raise CliError(f"{flag_name} local file does not exist: {path}")
    if not path.is_file():
        raise CliError(f"{flag_name} must point to a file: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise CliError(
            f"{flag_name} local file must be an image. "
            f"Got MIME type: {mime_type or 'unknown'}"
        )

    size = path.stat().st_size
    if size > MAX_LOCAL_IMAGE_BYTES:
        raise CliError(
            f"{flag_name} local file is too large ({size} bytes). "
            f"Max supported size for inline upload is {MAX_LOCAL_IMAGE_BYTES} bytes."
        )

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def normalize_image_ref(item: str, flag_name: str) -> str:
    if item.startswith(("http://", "https://", "data:")):
        return item

    path = file_uri_to_path(item) if item.startswith("file://") else Path(item).expanduser()
    return local_image_to_data_url(path, flag_name)


def normalize_image_refs(urls: list[str] | None, flag_name: str) -> list[str] | None:
    if not urls:
        return None
    return [normalize_image_ref(item, flag_name) for item in urls]


def normalize_video_image_refs(urls: list[str] | None, flag_name: str) -> list[str] | None:
    if not urls:
        return None
    normalized: list[str] = []
    for item in urls:
        if item.startswith(("http://", "https://")):
            normalized.append(item)
            continue
        raise CliError(
            f"{flag_name} for video generation currently requires a public http/https URL. "
            f"Local file paths, file:// URLs, and inline uploads are not supported by ArtsAPI video on 2026-03-22: {item}"
        )
    return normalized


def build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = base_url.rstrip("/") + path
    if not params:
        return url
    query = urllib.parse.urlencode(params)
    return f"{url}?{query}"


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)


def extract_error_message(data: Any) -> str | None:
    if isinstance(data, dict):
        if isinstance(data.get("msg"), str):
            return data["msg"]
        error_obj = data.get("error")
        if isinstance(error_obj, dict) and isinstance(error_obj.get("message"), str):
            return error_obj["message"]
        if isinstance(error_obj, str):
            return error_obj
    return None


def has_api_error(data: Any) -> bool:
    return isinstance(data, dict) and "code" in data and data.get("code") != 0


def auth_header(api_key: str) -> str:
    token = api_key.strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
    api_key: str | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if api_key:
        headers["Authorization"] = auth_header(api_key)

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise CliError(f"Network request failed: {exc}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CliError(f"API did not return JSON. Raw body: {body}") from exc


def ensure_output_dir(save_dir_raw: str | None) -> Path:
    output_dir = Path(save_dir_raw).expanduser() if save_dir_raw else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def guess_image_extension_from_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return ".png"


def guess_extension_from_content_type(content_type: str, url: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(normalized) if normalized else None
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed:
        return guessed

    parsed = urllib.parse.urlparse(url)
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix
    return suffix or ".bin"


def collect_b64_json_strings(data: Any, found: list[str] | None = None) -> list[str]:
    results = found if found is not None else []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "b64_json" and isinstance(value, str) and value.strip():
                results.append(value.strip())
            else:
                collect_b64_json_strings(value, results)
    elif isinstance(data, list):
        for item in data:
            collect_b64_json_strings(item, results)
    return results


def collect_http_urls(data: Any, found: list[str] | None = None) -> list[str]:
    results = found if found is not None else []
    if isinstance(data, dict):
        for value in data.values():
            collect_http_urls(value, results)
    elif isinstance(data, list):
        for item in data:
            collect_http_urls(item, results)
    elif isinstance(data, str) and data.startswith(("http://", "https://")):
        if data not in results:
            results.append(data)
    return results


def decode_b64_payload(raw: str) -> bytes:
    payload = raw.split(",", 1)[1] if raw.startswith("data:") and "," in raw else raw
    return base64.b64decode(payload, validate=False)


def save_b64_payloads(items: list[str], output_dir: Path, prefix: str) -> tuple[list[str], list[str]]:
    saved_files: list[str] = []
    errors: list[str] = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    for index, item in enumerate(items, start=1):
        try:
            binary = decode_b64_payload(item)
        except Exception as exc:
            errors.append(f"Failed to decode b64_json #{index}: {exc}")
            continue

        extension = guess_image_extension_from_bytes(binary)
        target = output_dir / f"{prefix}-{timestamp}-{index}{extension}"
        target.write_bytes(binary)
        saved_files.append(str(target))

    return saved_files, errors


def download_asset(url: str, output_dir: Path, prefix: str, index: int, timeout: int) -> tuple[str | None, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        return None, f"Failed to download asset #{index} ({url}): HTTP {exc.code}: {body or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"Failed to download asset #{index} ({url}): {exc}"

    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type.startswith("application/json") or normalized_type.startswith("text/"):
        text_body = body.decode("utf-8", errors="replace").strip()
        return None, f"Asset URL #{index} did not return a binary file: {text_body or normalized_type or 'unknown response'}"

    extension = guess_extension_from_content_type(content_type, url)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    target = output_dir / f"{prefix}-{timestamp}-{index}{extension}"
    target.write_bytes(body)
    return str(target), None


def attach_local_artifacts(response: Any, artifacts: dict[str, Any]) -> Any:
    if isinstance(response, dict):
        updated = dict(response)
        updated["_local_artifacts"] = artifacts
        return updated
    return {"response": response, "_local_artifacts": artifacts}


def maybe_save_local_outputs(
    response: Any,
    *,
    save_local: bool,
    save_dir_raw: str | None,
    timeout: int,
    prefix: str,
) -> Any:
    if not save_local:
        return response

    output_dir = ensure_output_dir(save_dir_raw)
    artifacts: dict[str, Any] = {
        "save_dir": str(output_dir),
        "saved_files": [],
        "save_errors": [],
    }

    b64_items = collect_b64_json_strings(response)
    if b64_items:
        saved_files, errors = save_b64_payloads(b64_items, output_dir, prefix)
        artifacts["strategy"] = "b64_json"
        artifacts["saved_files"].extend(saved_files)
        artifacts["save_errors"].extend(errors)
        return attach_local_artifacts(response, artifacts)

    urls = collect_http_urls(response)
    if urls:
        artifacts["strategy"] = "url_download"
        for index, url in enumerate(urls, start=1):
            saved_file, error = download_asset(url, output_dir, prefix, index, timeout)
            if saved_file:
                artifacts["saved_files"].append(saved_file)
            if error:
                artifacts["save_errors"].append(error)
        return attach_local_artifacts(response, artifacts)

    artifacts["strategy"] = "not_found"
    artifacts["save_errors"].append("No b64_json payload or downloadable asset URL was found in the API response.")
    return attach_local_artifacts(response, artifacts)


def resolve_runtime_settings(args: argparse.Namespace, require_api_key: bool) -> tuple[Path, str, str | None]:
    config_path = Path(args.config).expanduser()
    file_values = load_env_file(config_path)

    base_url = (
        args.base_url
        or os.environ.get("ARTSAPI_BASE_URL")
        or file_values.get("ARTSAPI_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")

    api_key = (
        args.api_key
        or os.environ.get("ARTSAPI_API_KEY")
        or file_values.get("ARTSAPI_API_KEY")
    )

    if require_api_key and is_placeholder(api_key):
        raise CliError(
            "Missing ArtsAPI key. Fill ARTSAPI_API_KEY in "
            f"{config_path} or pass --api-key."
        )

    return config_path, base_url, api_key


def cmd_models(args: argparse.Namespace) -> int:
    _, base_url, _ = resolve_runtime_settings(args, require_api_key=False)
    params = {
        "page": 1,
        "pageSize": args.page_size,
        "keyword": args.keyword or "",
    }
    if args.type:
        params["type"] = args.type

    url = build_url(base_url, "/admin/index/model-list", params)
    response = request_json("GET", url, timeout=args.timeout)
    print(pretty_json(response))
    return 0 if not has_api_error(response) else 1


def cmd_image(args: argparse.Namespace) -> int:
    _, base_url, api_key = resolve_runtime_settings(args, require_api_key=True)
    image_refs = normalize_image_refs(args.image_urls, "--image-url")

    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
    }
    if args.n is not None:
        payload["n"] = args.n
    if args.size:
        payload["size"] = args.size
    if args.seed is not None:
        payload["seed"] = args.seed
    if args.negative_prompt:
        payload["negative_prompt"] = args.negative_prompt
    if args.watermark:
        payload["watermark"] = True
    if args.response_format:
        payload["response_format"] = args.response_format
    if image_refs:
        payload["image"] = image_refs[0] if len(image_refs) == 1 else image_refs

    extra = parse_extra_json(args.extra_json)
    if extra:
        payload["extra"] = extra

    response = request_json(
        "POST",
        build_url(base_url, "/v1/images/generations"),
        payload=payload,
        timeout=args.timeout,
        api_key=api_key,
    )
    output = response
    if not has_api_error(response):
        output = maybe_save_local_outputs(
            response,
            save_local=args.save_local,
            save_dir_raw=args.save_dir,
            timeout=args.timeout,
            prefix="artsapi-image",
        )
    print(pretty_json(output))
    return 0 if not has_api_error(response) else 1


def extract_task_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for container in (response, response.get("data")):
        if isinstance(container, dict) and isinstance(container.get("task_id"), str):
            return container["task_id"]
    return None


def extract_status(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for container in (response, response.get("data")):
        if isinstance(container, dict) and isinstance(container.get("status"), str):
            return container["status"]
    return None


def is_success_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in {"completed", "succeeded", "success"}


def is_failure_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in {"failed", "error", "cancelled", "canceled"}


def fetch_video_status(base_url: str, api_key: str, task_id: str, timeout: int) -> Any:
    return request_json(
        "GET",
        build_url(base_url, f"/v1/video/generations/{task_id}"),
        timeout=timeout,
        api_key=api_key,
    )


def watch_video_status(
    base_url: str,
    api_key: str,
    task_id: str,
    *,
    timeout: int,
    poll_interval: int,
    max_wait: int,
) -> tuple[Any, bool]:
    deadline = time.time() + max_wait
    latest: Any = None
    while time.time() <= deadline:
        latest = fetch_video_status(base_url, api_key, task_id, timeout)
        status = extract_status(latest)
        if is_success_status(status) or is_failure_status(status):
            return latest, is_success_status(status)
        time.sleep(poll_interval)
    return latest, False


def cmd_video(args: argparse.Namespace) -> int:
    _, base_url, api_key = resolve_runtime_settings(args, require_api_key=True)
    image_refs = normalize_video_image_refs(args.image_urls, "--image-url")

    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
    }
    if args.duration is not None:
        payload["duration"] = args.duration
    if args.resolution:
        payload["resolution"] = args.resolution
    if args.ratio:
        payload["ratio"] = args.ratio
    if args.negative_prompt:
        payload["negative_prompt"] = args.negative_prompt
    if image_refs:
        payload["images"] = image_refs

    extra = parse_extra_json(args.extra_json)
    if extra:
        payload["extra"] = extra

    submit_response = request_json(
        "POST",
        build_url(base_url, "/v1/video/generations"),
        payload=payload,
        timeout=args.timeout,
        api_key=api_key,
    )
    if has_api_error(submit_response):
        print(pretty_json(submit_response))
        return 1

    if args.no_poll:
        print(pretty_json(submit_response))
        return 0

    task_id = extract_task_id(submit_response)
    if not task_id:
        print(pretty_json(submit_response))
        return 0

    final_response, completed = watch_video_status(
        base_url,
        api_key or "",
        task_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
    )
    output = final_response
    if completed and not has_api_error(final_response):
        output = maybe_save_local_outputs(
            final_response,
            save_local=args.save_local,
            save_dir_raw=args.save_dir,
            timeout=args.timeout,
            prefix="artsapi-video",
        )
    print(pretty_json(output))
    return 0 if completed and not has_api_error(final_response) else 1


def cmd_status(args: argparse.Namespace) -> int:
    _, base_url, api_key = resolve_runtime_settings(args, require_api_key=True)

    if not args.watch:
        response = fetch_video_status(base_url, api_key or "", args.task_id, args.timeout)
        output = response
        if is_success_status(extract_status(response)) and not has_api_error(response):
            output = maybe_save_local_outputs(
                response,
                save_local=args.save_local,
                save_dir_raw=args.save_dir,
                timeout=args.timeout,
                prefix="artsapi-video",
            )
        print(pretty_json(output))
        return 0 if not has_api_error(response) else 1

    final_response, completed = watch_video_status(
        base_url,
        api_key or "",
        args.task_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
    )
    output = final_response
    if completed and not has_api_error(final_response):
        output = maybe_save_local_outputs(
            final_response,
            save_local=args.save_local,
            save_dir_raw=args.save_dir,
            timeout=args.timeout,
            prefix="artsapi-video",
        )
    print(pretty_json(output))
    return 0 if completed and not has_api_error(final_response) else 1


def add_save_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--save-local",
        action="store_true",
        help="Decode/download generated assets into local files and return their saved paths.",
    )
    parser.add_argument(
        "--save-dir",
        help=f"Output directory for locally saved results. Default: {DEFAULT_OUTPUT_DIR}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ArtsAPI helper for image generation and image-to-video tasks."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Config file path. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--api-key",
        help="ArtsAPI key. Overrides ARTSAPI_API_KEY from config/environment.",
    )
    parser.add_argument(
        "--base-url",
        help=f"Override base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds. Default: 120",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    models_parser = subparsers.add_parser("models", help="List currently available models.")
    models_parser.add_argument("--type", choices=["image", "video"], help="Filter by type.")
    models_parser.add_argument("--keyword", help="Optional search keyword.")
    models_parser.add_argument("--page-size", type=int, default=100, help="Page size. Default: 100")
    models_parser.set_defaults(func=cmd_models)

    image_parser = subparsers.add_parser("image", help="Submit an image generation request.")
    image_parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL, help="Image model name.")
    image_parser.add_argument("--prompt", required=True, help="Prompt text.")
    image_parser.add_argument(
        "--image-url",
        action="append",
        dest="image_urls",
        help="Reference image URL, data URL, or local image path. Repeat for multiple images.",
    )
    image_parser.add_argument("--n", type=int, default=1, help="Number of images. Default: 1")
    image_parser.add_argument("--size", help="Image size, for example 1024x1024.")
    image_parser.add_argument("--seed", type=int, help="Seed value.")
    image_parser.add_argument("--negative-prompt", help="Negative prompt.")
    image_parser.add_argument("--watermark", action="store_true", help="Enable watermark.")
    image_parser.add_argument(
        "--response-format",
        choices=["url", "b64_json"],
        help="Response format.",
    )
    image_parser.add_argument(
        "--extra-json",
        help='Extra provider-native JSON object, for example: \'{"steps":30}\'',
    )
    add_save_args(image_parser)
    image_parser.set_defaults(func=cmd_image)

    video_parser = subparsers.add_parser("video", help="Submit an image-to-video request.")
    video_parser.add_argument("--model", default=DEFAULT_VIDEO_MODEL, help="Video model name.")
    video_parser.add_argument("--prompt", required=True, help="Prompt text.")
    video_parser.add_argument(
        "--image-url",
        action="append",
        dest="image_urls",
        help="Reference image URL. For video generation, use public http/https URLs and repeat for first/last frame or multiple references.",
    )
    video_parser.add_argument("--duration", type=int, help="Duration in seconds.")
    video_parser.add_argument("--resolution", help="Resolution, for example 720p or 1080p.")
    video_parser.add_argument("--ratio", help="Aspect ratio, for example 16:9.")
    video_parser.add_argument("--negative-prompt", help="Negative prompt.")
    video_parser.add_argument(
        "--extra-json",
        help='Extra provider-native JSON object, for example: \'{"fps":30}\'',
    )
    video_parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Only submit the task and print task_id/status without polling.",
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
    add_save_args(video_parser)
    video_parser.set_defaults(func=cmd_video)

    status_parser = subparsers.add_parser("status", help="Check a video task status.")
    status_parser.add_argument("task_id", help="Task identifier.")
    status_parser.add_argument("--watch", action="store_true", help="Poll until completion or timeout.")
    status_parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Polling interval in seconds. Default: 5",
    )
    status_parser.add_argument(
        "--max-wait",
        type=int,
        default=900,
        help="Maximum polling time in seconds. Default: 900",
    )
    add_save_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
