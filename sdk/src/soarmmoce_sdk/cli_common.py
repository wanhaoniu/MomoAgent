from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .json_utils import to_jsonable


def cli_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Could not parse boolean value: {value!r}")


def _print_payload(payload: dict[str, Any]) -> None:
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def print_success(result: Any) -> None:
    _print_payload({"ok": True, "result": result, "error": None})


def print_error(exc: Exception) -> None:
    _print_payload(
        {
            "ok": False,
            "result": None,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    )


def run_and_print(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        print_success(fn(*args, **kwargs))
    except SystemExit:
        raise
    except Exception as exc:
        print_error(exc)
        raise SystemExit(1) from exc


__all__ = ["cli_bool", "print_error", "print_success", "run_and_print"]
