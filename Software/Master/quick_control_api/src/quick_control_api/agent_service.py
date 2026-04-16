from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[5]
MASTER_ROOT = REPO_ROOT / "Software" / "Master"
SDK_SRC = REPO_ROOT / "sdk" / "src"

for _extra_path in (str(MASTER_ROOT), str(SDK_SRC)):
    if not _extra_path:
        continue
    norm = os.path.normpath(_extra_path)
    sys.path[:] = [path for path in sys.path if os.path.normpath(path or os.curdir) != norm]
    sys.path.insert(0, _extra_path)

from momo_agent.config import load_config as load_momo_agent_config
from momo_agent.openclaw_client import OpenClawReply, build_openclaw_client

from .errors import QuickControlError
from .remote_tts import RemoteTtsMonitor

OPENCLAW_CHAT_STREAM_BRIDGE_SCRIPT = (
    REPO_ROOT / "Software" / "Master" / "openclaw_local" / "openclaw_gateway_chat_stream_bridge.js"
)


@dataclass
class AgentTurnRecord:
    kind: str = "idle"
    status: str = "idle"
    prompt: str = ""
    reply: str = ""
    error: str = ""
    session_id: str = ""
    bridge_session_key: str = ""
    openclaw_elapsed_sec: float = 0.0
    bridge_timing: dict[str, float] = field(default_factory=dict)
    tts: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


class AgentService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._openclaw_config = load_momo_agent_config().openclaw
        self._client = None
        self._remote_tts = RemoteTtsMonitor()
        self._busy = False
        self._last_error = ""
        self._last_turn = AgentTurnRecord(updated_at=time.time())

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _ensure_client_locked(self):
        if not bool(self._openclaw_config.enabled):
            raise QuickControlError("AGENT_DISABLED", "OpenClaw agent is disabled", 503)
        if self._client is None:
            self._client = build_openclaw_client(self._openclaw_config)
        return self._client

    def _resolve_node_bin(self) -> str:
        if self._openclaw_config.node_bin:
            return str(self._openclaw_config.node_bin).strip()
        return shutil.which("node") or shutil.which("nodejs") or ""

    @staticmethod
    def _extract_bridge_timing(reply: OpenClawReply) -> dict[str, float]:
        payload = reply.raw_payload
        if not isinstance(payload, dict):
            return {}
        bridge = payload.get("bridge")
        if not isinstance(bridge, dict):
            return {}
        timing = bridge.get("timing")
        if not isinstance(timing, dict):
            return {}
        out: dict[str, float] = {}
        for key in ("accept_ms", "wait_ms", "history_ms", "total_ms"):
            try:
                out[key] = float(timing.get(key, 0.0) or 0.0)
            except Exception:
                out[key] = 0.0
        return out

    def _turn_payload_locked(self) -> dict[str, Any]:
        return asdict(self._last_turn)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            client = self._client
            session_id = str(client.session_id).strip() if client is not None else ""
            bridge_key = str(client.bridge_session_key).strip() if client is not None else ""
            return {
                "enabled": bool(self._openclaw_config.enabled),
                "busy": bool(self._busy),
                "thinking": str(self._openclaw_config.thinking or "").strip(),
                "skill_name": str(self._openclaw_config.skill_name or "").strip(),
                "local_mode": bool(self._openclaw_config.local_mode),
                "robot_mode": bool(self._openclaw_config.robot_mode),
                "timeout_sec": float(self._openclaw_config.timeout_sec),
                "session_id": session_id,
                "bridge_session_key": bridge_key,
                "last_error": str(self._last_error or "").strip(),
                "tts": self._remote_tts.status_payload(),
                "last_turn": self._turn_payload_locked(),
            }

    def last_turn_payload(self) -> dict[str, Any]:
        with self._lock:
            return self._turn_payload_locked()

    def _run_turn(self, *, kind: str, prompt: str) -> dict[str, Any]:
        message = str(prompt or "").strip()
        if not message:
            raise QuickControlError("INVALID_ARGUMENT", "Agent prompt is empty", 400)

        with self._lock:
            if self._busy:
                raise QuickControlError("AGENT_BUSY", "Agent is already processing another turn", 409)
            self._busy = True
            client = self._ensure_client_locked()

        started = time.perf_counter()
        try:
            reply = client.ask(message)
            elapsed = time.perf_counter() - started
            bridge_timing = self._extract_bridge_timing(reply)
            turn = AgentTurnRecord(
                kind=str(kind or "ask"),
                status="ok",
                prompt=message,
                reply=str(reply.text or "").strip(),
                error="",
                session_id=str(reply.session_id or "").strip(),
                bridge_session_key=str(client.bridge_session_key or "").strip(),
                openclaw_elapsed_sec=float(elapsed),
                bridge_timing=bridge_timing,
                tts={"requested": False},
                updated_at=time.time(),
            )
            with self._lock:
                self._last_turn = turn
                self._last_error = ""
                self._busy = False
                return {
                    "turn": asdict(turn),
                    "status": self.status_payload(),
                }
        except QuickControlError:
            with self._lock:
                self._busy = False
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            with self._lock:
                self._last_error = str(exc).strip() or "Agent turn failed"
                self._last_turn = AgentTurnRecord(
                    kind=str(kind or "ask"),
                    status="error",
                    prompt=message,
                    reply="",
                    error=self._last_error,
                    session_id=str(client.session_id or "").strip(),
                    bridge_session_key=str(client.bridge_session_key or "").strip(),
                    openclaw_elapsed_sec=float(elapsed),
                    bridge_timing={},
                    tts={"requested": False},
                    updated_at=time.time(),
                )
                self._busy = False
            raise QuickControlError("AGENT_FAILED", self._last_error, 500) from exc

    def ask(self, message: str) -> dict[str, Any]:
        return self._run_turn(kind="ask", prompt=message)

    def warmup(self, prompt: str = "请只回复“就绪”。") -> dict[str, Any]:
        return self._run_turn(kind="warmup", prompt=prompt)

    def build_stream_turn_spec(self, *, kind: str, prompt: str) -> dict[str, Any]:
        message = str(prompt or "").strip()
        if not message:
            raise QuickControlError("INVALID_ARGUMENT", "Agent prompt is empty", 400)
        if self._openclaw_config.local_mode:
            return {
                "ok": False,
                "reason": "OpenClaw local mode does not support gateway chat streaming",
            }

        node_bin = self._resolve_node_bin()
        if not node_bin:
            return {
                "ok": False,
                "reason": "Node.js is unavailable for the OpenClaw chat stream bridge",
            }
        if not OPENCLAW_CHAT_STREAM_BRIDGE_SCRIPT.is_file():
            return {
                "ok": False,
                "reason": f"OpenClaw chat stream bridge is missing: {OPENCLAW_CHAT_STREAM_BRIDGE_SCRIPT}",
            }

        with self._lock:
            if self._busy:
                raise QuickControlError("AGENT_BUSY", "Agent is already processing another turn", 409)
            client = self._ensure_client_locked()
            self._busy = True
            return {
                "ok": True,
                "kind": str(kind or "ask"),
                "prompt": message,
                "command": [node_bin, str(OPENCLAW_CHAT_STREAM_BRIDGE_SCRIPT)],
                "stdin_payload": {
                    "id": str(uuid.uuid4()),
                    "op": "chat_stream_turn",
                    "message": message,
                    "session_key": str(client.bridge_session_key or "").strip(),
                    "thinking": self._openclaw_config.thinking or "",
                    "timeout_sec": float(self._openclaw_config.timeout_sec),
                },
                "session_id": str(client.session_id or "").strip(),
                "bridge_session_key": str(client.bridge_session_key or "").strip(),
            }

    def complete_stream_turn(
        self,
        *,
        kind: str,
        prompt: str,
        reply: str,
        session_id: str,
        bridge_session_key: str,
        openclaw_elapsed_sec: float,
        bridge_timing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        message = str(prompt or "").strip()
        reply_text = str(reply or "").strip()
        timing = dict(bridge_timing or {})
        with self._lock:
            client = self._client
            if client is not None:
                client.update_session_state(
                    session_id=str(session_id or "").strip(),
                    bridge_session_key=str(bridge_session_key or "").strip(),
                )
            turn = AgentTurnRecord(
                kind=str(kind or "ask"),
                status="ok",
                prompt=message,
                reply=reply_text,
                error="",
                session_id=str(session_id or "").strip(),
                bridge_session_key=str(bridge_session_key or "").strip(),
                openclaw_elapsed_sec=float(openclaw_elapsed_sec or 0.0),
                bridge_timing=timing,
                tts={"requested": False},
                updated_at=time.time(),
            )
            self._last_turn = turn
            self._last_error = ""
            self._busy = False
            return {
                "turn": asdict(turn),
                "status": self.status_payload(),
            }

    def fail_stream_turn(
        self,
        *,
        kind: str,
        prompt: str,
        error: str,
        session_id: str = "",
        bridge_session_key: str = "",
        openclaw_elapsed_sec: float = 0.0,
        bridge_timing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        message = str(prompt or "").strip()
        error_text = str(error or "").strip() or "Agent stream failed"
        timing = dict(bridge_timing or {})
        with self._lock:
            client = self._client
            resolved_session_id = str(session_id or "").strip()
            resolved_bridge_session_key = str(bridge_session_key or "").strip()
            if client is not None:
                if not resolved_session_id:
                    resolved_session_id = str(client.session_id or "").strip()
                if not resolved_bridge_session_key:
                    resolved_bridge_session_key = str(client.bridge_session_key or "").strip()
            self._last_error = error_text
            self._last_turn = AgentTurnRecord(
                kind=str(kind or "ask"),
                status="error",
                prompt=message,
                reply="",
                error=error_text,
                session_id=resolved_session_id,
                bridge_session_key=resolved_bridge_session_key,
                openclaw_elapsed_sec=float(openclaw_elapsed_sec or 0.0),
                bridge_timing=timing,
                tts={"requested": False},
                updated_at=time.time(),
            )
            self._busy = False
            return self._turn_payload_locked()

    def reset_session(self) -> dict[str, Any]:
        with self._lock:
            if self._busy:
                raise QuickControlError("AGENT_BUSY", "Agent is already processing another turn", 409)
            client = self._ensure_client_locked()
            client.reset_session()
            self._last_error = ""
            self._last_turn = AgentTurnRecord(
                kind="reset_session",
                status="ok",
                prompt="",
                reply="",
                error="",
                session_id=str(client.session_id or "").strip(),
                bridge_session_key=str(client.bridge_session_key or "").strip(),
                openclaw_elapsed_sec=0.0,
                bridge_timing={},
                tts={"requested": False},
                updated_at=time.time(),
            )
            return self.status_payload()

    def tts_status_payload(self) -> dict[str, Any]:
        return self._remote_tts.status_payload()

    def build_tts_stream_spec(self, text: str) -> dict[str, Any]:
        return self._remote_tts.build_stream_spec(text)

    def set_last_turn_tts_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        payload = dict(summary or {})
        with self._lock:
            self._last_turn.tts = payload
            self._last_turn.updated_at = time.time()
            return self._turn_payload_locked()
