#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    import websockets
except Exception as exc:  # pragma: no cover - dependency is optional in this repo
    websockets = None
    WEBSOCKETS_IMPORT_ERROR = exc
else:
    WEBSOCKETS_IMPORT_ERROR = None


DEFAULT_BASE_VAR = "AUTOGRASP_VLM_API_BASE"
DEFAULT_KEY_VAR = "AUTOGRASP_VLM_API_KEY"
DEFAULT_MODEL_VAR = "AUTOGRASP_VLM_MODEL"
DEFAULT_MODELS = ("gpt-realtime", "gpt-realtime-mini", "gpt-4o-realtime-preview")


@dataclass
class RestResult:
    ok: bool
    status_code: int | None
    model_ids: list[str]
    error: str = ""


@dataclass
class WsResult:
    model: str
    beta_header: bool
    ok: bool
    status_code: int | None
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test whether an OpenAI-compatible proxy exposes the official Realtime WebSocket "
            "endpoint required by OpenAI Realtime API clients."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file to read. Defaults to ./.env",
    )
    parser.add_argument(
        "--base-var",
        default=DEFAULT_BASE_VAR,
        help=f"Environment variable name for the OpenAI-compatible base URL. Defaults to {DEFAULT_BASE_VAR}.",
    )
    parser.add_argument(
        "--key-var",
        default=DEFAULT_KEY_VAR,
        help=f"Environment variable name for the API key. Defaults to {DEFAULT_KEY_VAR}.",
    )
    parser.add_argument(
        "--model-var",
        default=DEFAULT_MODEL_VAR,
        help=f"Environment variable name for the default model. Defaults to {DEFAULT_MODEL_VAR}.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Extra model id to probe. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Timeout in seconds for REST and WebSocket connect attempts. Defaults to 15.",
    )
    return parser.parse_args()


def load_simple_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def getenv_merged(name: str, dotenv_values: dict[str, str], default: str = "") -> str:
    if name in os.environ:
        return str(os.environ[name]).strip()
    if name in dotenv_values:
        return str(dotenv_values[name]).strip()
    return default


def mask_secret(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def unique_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def derive_ws_url(base_url: str, model: str) -> str:
    parsed = urllib_parse.urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/realtime"
    query = urllib_parse.urlencode({"model": model})
    return urllib_parse.urlunparse((scheme, parsed.netloc, path, "", query, ""))


def rest_get_models(base_url: str, api_key: str, timeout: float) -> RestResult:
    url = f"{base_url.rstrip('/')}/models"
    request = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(
            request,
            context=ssl.create_default_context(),
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            model_ids = []
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                model_ids = [
                    str(item.get("id"))
                    for item in payload["data"]
                    if isinstance(item, dict) and item.get("id")
                ]
            return RestResult(
                ok=True,
                status_code=int(getattr(response, "status", 200)),
                model_ids=model_ids,
            )
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        return RestResult(
            ok=False,
            status_code=int(exc.code),
            model_ids=[],
            error=detail or f"HTTP {exc.code}",
        )
    except Exception as exc:
        return RestResult(ok=False, status_code=None, model_ids=[], error=f"{type(exc).__name__}: {exc}")


def _extract_ws_status(exc: Exception) -> tuple[int | None, str]:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    detail = ""
    if response is not None:
        status_code = status_code if status_code is not None else getattr(response, "status_code", None)
        body = getattr(response, "body", None)
        if isinstance(body, (bytes, bytearray)):
            detail = body.decode("utf-8", "replace").strip()
        elif body is not None:
            detail = str(body).strip()
    return status_code, detail


async def ws_probe_once(url: str, api_key: str, beta_header: bool, timeout: float) -> tuple[bool, int | None, str]:
    if websockets is None:
        raise RuntimeError(f"websockets import failed: {WEBSOCKETS_IMPORT_ERROR}")
    headers = {"Authorization": f"Bearer {api_key}"}
    if beta_header:
        headers["OpenAI-Beta"] = "realtime=v1"
    ssl_context = ssl.create_default_context() if url.startswith("wss://") else None
    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=timeout,
            close_timeout=min(timeout, 5.0),
            ssl=ssl_context,
        ):
            return True, None, "handshake accepted"
    except Exception as exc:
        status_code, detail = _extract_ws_status(exc)
        message = str(exc).strip() or f"{type(exc).__name__}"
        if detail and detail not in message:
            message = f"{message}; body={detail}"
        return False, status_code, message


async def ws_probe_models(base_url: str, api_key: str, models: list[str], timeout: float) -> list[WsResult]:
    results: list[WsResult] = []
    for model in models:
        url = derive_ws_url(base_url, model)
        for beta_header in (False, True):
            ok, status_code, detail = await ws_probe_once(url, api_key, beta_header, timeout)
            results.append(
                WsResult(
                    model=model,
                    beta_header=beta_header,
                    ok=ok,
                    status_code=status_code,
                    detail=detail,
                )
            )
    return results


def print_summary(
    env_file: Path,
    base_var: str,
    key_var: str,
    model_var: str,
    base_url: str,
    api_key: str,
    env_model: str,
    rest_result: RestResult,
    ws_results: list[WsResult],
) -> None:
    realtime_like = [
        model_id
        for model_id in rest_result.model_ids
        if "realtime" in model_id.lower() or "audio" in model_id.lower()
    ]

    print(f"env_file: {env_file}")
    print(f"base_var: {base_var}")
    print(f"key_var: {key_var}")
    print(f"model_var: {model_var}")
    print(f"base_url: {base_url}")
    print(f"api_key: {mask_secret(api_key)}")
    print(f"env_model: {env_model or '<empty>'}")
    print()

    if rest_result.ok:
        print(f"REST /models: OK (HTTP {rest_result.status_code})")
        print(f"models_total: {len(rest_result.model_ids)}")
        if realtime_like:
            print("realtime_or_audio_models:")
            for model_id in realtime_like:
                print(f"  - {model_id}")
        else:
            print("realtime_or_audio_models: none")
    else:
        print(f"REST /models: FAIL ({rest_result.status_code or 'no-status'})")
        print(f"rest_error: {rest_result.error}")
    print()

    if websockets is None:
        print(f"WebSocket probe skipped: websockets import failed: {WEBSOCKETS_IMPORT_ERROR}")
        return

    print("WS official endpoint probes:")
    for result in ws_results:
        path_hint = derive_ws_url(base_url, result.model)
        status_text = f"HTTP {result.status_code}" if result.status_code is not None else "no-status"
        beta_text = "yes" if result.beta_header else "no"
        outcome = "OK" if result.ok else "FAIL"
        print(
            f"  - model={result.model} beta_header={beta_text} outcome={outcome} "
            f"status={status_text} url={path_hint}"
        )
        print(f"    detail={result.detail}")
    print()

    ws_success = any(result.ok for result in ws_results)
    if rest_result.ok and not ws_success:
        print("conclusion: REST compatibility is working, but the official Realtime WebSocket endpoint is not exposed.")
        print("conclusion_detail: This proxy is not OpenAI Realtime WebSocket compatible for voice/ws clients in its current form.")
    elif rest_result.ok and ws_success:
        print("conclusion: Realtime WebSocket handshake succeeded on at least one probe.")
    else:
        print("conclusion: REST access failed before Realtime compatibility could be established.")


async def async_main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve()
    dotenv_values = load_simple_dotenv(env_file)

    base_url = getenv_merged(args.base_var, dotenv_values)
    api_key = getenv_merged(args.key_var, dotenv_values)
    env_model = getenv_merged(args.model_var, dotenv_values)

    if not base_url:
        print(f"Missing base URL. Checked {args.base_var} in environment and {env_file}.", file=sys.stderr)
        return 2
    if not api_key:
        print(f"Missing API key. Checked {args.key_var} in environment and {env_file}.", file=sys.stderr)
        return 2

    rest_result = rest_get_models(base_url, api_key, args.timeout)

    candidate_models = list(DEFAULT_MODELS)
    if env_model:
        candidate_models.append(env_model)
    if rest_result.ok:
        candidate_models.extend(
            model_id
            for model_id in rest_result.model_ids
            if "realtime" in model_id.lower() or "audio" in model_id.lower()
        )
    candidate_models.extend(args.model)
    probe_models = unique_keep_order(candidate_models)

    ws_results = await ws_probe_models(base_url, api_key, probe_models, args.timeout)
    print_summary(
        env_file=env_file,
        base_var=args.base_var,
        key_var=args.key_var,
        model_var=args.model_var,
        base_url=base_url,
        api_key=api_key,
        env_model=env_model,
        rest_result=rest_result,
        ws_results=ws_results,
    )

    return 0 if any(result.ok for result in ws_results) else 1


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
