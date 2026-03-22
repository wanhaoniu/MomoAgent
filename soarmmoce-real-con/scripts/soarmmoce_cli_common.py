#!/usr/bin/env python3
"""Shared CLI helpers for soarmMoce control scripts."""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable, TypeVar

from soarmmoce_sdk import to_jsonable


ResultT = TypeVar("ResultT")


def cli_bool(value: str) -> bool:
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value!r}")


def success_payload(data: Any) -> dict[str, Any]:
    return {"ok": True, "result": to_jsonable(data), "error": None}


def error_payload(exc: Exception) -> dict[str, Any]:
    payload = {
        "ok": False,
        "result": None,
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }
    details = getattr(exc, "details", None)
    if details is not None:
        payload["error"]["details"] = to_jsonable(details)
    return payload


def print_success(data: Any) -> None:
    print(json.dumps(success_payload(data), ensure_ascii=False, indent=2))


def print_error(exc: Exception) -> None:
    print(json.dumps(error_payload(exc), ensure_ascii=False, indent=2))


def run_and_print(action: Callable[[], ResultT]) -> None:
    try:
        print_success(action())
    except Exception as exc:
        print_error(exc)
