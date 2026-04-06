from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_REMOTE_TTS_BASE_URL = "http://192.168.66.92:7999"
DEFAULT_REMOTE_TTS_MODEL = "qwen/qwen3.5-35b-a3b"
DEFAULT_REMOTE_TTS_SYSTEM_PROMPT = (
    "You are a text-to-speech bridge. Repeat the user input verbatim so it can be spoken aloud. "
    "Do not answer, explain, translate, summarize, add markdown, or add quotes. "
    "Preserve the original language and punctuation exactly."
)
DEFAULT_REMOTE_TTS_TIMEOUT_SEC = 30.0
DEFAULT_REMOTE_TTS_MAX_CHARS = 400
REMOTE_TTS_HEALTH_TIMEOUT_SEC = 1.2
REMOTE_TTS_HEALTH_CACHE_SEC = 5.0
REMOTE_TTS_BRIDGE_SCRIPT = REPO_ROOT / "Software" / "Master" / "quick_control_api" / "tts_stream_bridge.js"


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _read_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _read_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except Exception:
        value = int(default)
    return max(int(minimum), value)


@dataclass(frozen=True)
class RemoteTtsConfig:
    enabled: bool
    base_url: str
    model: str
    system_prompt: str
    timeout_sec: float
    max_chars: int
    node_bin: str
    bridge_script: str


def load_remote_tts_config() -> RemoteTtsConfig:
    base_url = str(os.getenv("QUICK_CONTROL_TTS_BASE_URL", DEFAULT_REMOTE_TTS_BASE_URL) or "").strip()
    return RemoteTtsConfig(
        enabled=_read_bool("QUICK_CONTROL_TTS_ENABLED", True),
        base_url=base_url,
        model=str(os.getenv("QUICK_CONTROL_TTS_MODEL", DEFAULT_REMOTE_TTS_MODEL) or "").strip()
        or DEFAULT_REMOTE_TTS_MODEL,
        system_prompt=str(
            os.getenv("QUICK_CONTROL_TTS_SYSTEM_PROMPT", DEFAULT_REMOTE_TTS_SYSTEM_PROMPT) or ""
        ).strip()
        or DEFAULT_REMOTE_TTS_SYSTEM_PROMPT,
        timeout_sec=_read_float(
            "QUICK_CONTROL_TTS_TIMEOUT_SEC",
            DEFAULT_REMOTE_TTS_TIMEOUT_SEC,
            minimum=3.0,
        ),
        max_chars=_read_int("QUICK_CONTROL_TTS_MAX_CHARS", DEFAULT_REMOTE_TTS_MAX_CHARS, minimum=1),
        node_bin=str(os.getenv("QUICK_CONTROL_TTS_NODE_BIN", "") or "").strip(),
        bridge_script=str(REMOTE_TTS_BRIDGE_SCRIPT),
    )


class RemoteTtsMonitor:
    def __init__(self) -> None:
        self._config = load_remote_tts_config()
        self._lock = threading.RLock()
        self._last_error = ""
        self._last_health: dict[str, Any] = {}
        self._last_health_at = 0.0

    def _resolve_node_bin(self) -> str:
        if self._config.node_bin:
            return self._config.node_bin
        return shutil.which("node") or shutil.which("nodejs") or ""

    def configured(self) -> bool:
        return bool(self._config.base_url)

    def bridge_available(self) -> bool:
        return bool(self._resolve_node_bin()) and Path(self._config.bridge_script).is_file()

    def _health_url(self) -> str:
        return f"{self._config.base_url.rstrip('/')}/healthz"

    def _fetch_health_locked(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._last_health
            and (now - self._last_health_at) < REMOTE_TTS_HEALTH_CACHE_SEC
        ):
            return dict(self._last_health)

        if not self._config.enabled or not self.configured():
            self._last_health = {}
            self._last_health_at = now
            return {}

        opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
        request = urllib_request.Request(
            self._health_url(),
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            method="GET",
        )

        try:
            with opener.open(request, timeout=REMOTE_TTS_HEALTH_TIMEOUT_SEC) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib_error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self._last_error = str(exc).strip() or "Remote TTS health check failed"
            self._last_health = {
                "ok": False,
                "error": self._last_error,
            }
            self._last_health_at = now
            return dict(self._last_health)

        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "Remote TTS health payload is not a JSON object"}

        payload["checked_at"] = time.time()
        self._last_health = payload
        self._last_health_at = now
        if bool(payload.get("ok")):
            self._last_error = ""
        else:
            self._last_error = str(payload.get("error", "") or "").strip()
        return dict(self._last_health)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            health = self._fetch_health_locked(force=False)
            return {
                "enabled": bool(self._config.enabled),
                "configured": self.configured(),
                "available": bool(
                    self._config.enabled and self.configured() and self.bridge_available()
                ),
                "base_url": str(self._config.base_url or "").strip(),
                "model": str(self._config.model or "").strip(),
                "timeout_sec": float(self._config.timeout_sec),
                "max_chars": int(self._config.max_chars),
                "last_error": str(self._last_error or "").strip(),
                "health": health,
            }

    def build_stream_spec(self, text: str) -> dict[str, Any]:
        message = str(text or "").strip()
        summary: dict[str, Any] = {
            "requested": True,
            "ok": False,
            "base_url": str(self._config.base_url or "").strip(),
            "model": str(self._config.model or "").strip(),
            "input_chars": len(message),
            "error": "",
        }

        with self._lock:
            if not self._config.enabled:
                summary["error"] = "Remote TTS is disabled"
                return {
                    "ok": False,
                    "summary": summary,
                    "command": [],
                    "stdin_payload": {},
                }
            if not self.configured():
                summary["error"] = "Remote TTS base URL is empty"
                return {
                    "ok": False,
                    "summary": summary,
                    "command": [],
                    "stdin_payload": {},
                }
            if not self.bridge_available():
                summary["error"] = "Remote TTS bridge is unavailable because Node.js or the bridge script is missing"
                return {
                    "ok": False,
                    "summary": summary,
                    "command": [],
                    "stdin_payload": {},
                }
            if not message:
                summary["error"] = "Remote TTS input is empty"
                return {
                    "ok": False,
                    "summary": summary,
                    "command": [],
                    "stdin_payload": {},
                }
            if len(message) > int(self._config.max_chars):
                summary["error"] = (
                    "Remote TTS input is too long "
                    f"({len(message)} > {int(self._config.max_chars)} chars)"
                )
                return {
                    "ok": False,
                    "summary": summary,
                    "command": [],
                    "stdin_payload": {},
                }

            command = [
                self._resolve_node_bin(),
                str(Path(self._config.bridge_script).expanduser().resolve()),
            ]
            stdin_payload = {
                "op": "speak_text_stream",
                "base_url": str(self._config.base_url or "").strip(),
                "model": str(self._config.model or "").strip(),
                "system_prompt": str(self._config.system_prompt or "").strip(),
                "input": message,
                "timeout_sec": float(self._config.timeout_sec),
            }
            return {
                "ok": True,
                "summary": summary,
                "command": command,
                "stdin_payload": stdin_payload,
            }
