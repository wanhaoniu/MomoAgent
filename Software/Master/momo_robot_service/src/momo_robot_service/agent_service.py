from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
MASTER_ROOT = REPO_ROOT / "Software" / "Master"
SDK_SRC = REPO_ROOT / "sdk" / "src"

for _extra_path in (str(MASTER_ROOT), str(SDK_SRC)):
    if not _extra_path:
        continue
    norm = os.path.normpath(_extra_path)
    sys.path[:] = [path for path in sys.path if os.path.normpath(path or os.curdir) != norm]
    sys.path.insert(0, _extra_path)

from momo_agent.agent_client import AgentReply, ToolRequester, build_agent_client
from momo_agent.config import load_config as load_momo_agent_config

from .errors import MomoRobotError
from .remote_tts import RemoteTtsMonitor


@dataclass
class AgentTurnRecord:
    backend: str = "nanobot"
    kind: str = "idle"
    status: str = "idle"
    prompt: str = ""
    reply: str = ""
    error: str = ""
    session_id: str = ""
    agent_session_key: str = ""
    agent_elapsed_sec: float = 0.0
    tts: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


class AgentService:
    def __init__(
        self,
        *,
        tool_requester: ToolRequester | None = None,
        tool_request_timeout_sec: float = 6.0,
    ) -> None:
        self._lock = threading.RLock()
        self._config = load_momo_agent_config()
        self._agent_backend = "nanobot"
        self._nanobot_config = self._config.nanobot
        self._tool_requester = tool_requester
        self._tool_request_timeout_sec = max(2.0, float(tool_request_timeout_sec))
        self._client = None
        self._remote_tts = RemoteTtsMonitor()
        self._busy = False
        self._last_error = ""
        self._resolved_status_fields = {
            "thinking": str(self._nanobot_config.reasoning_effort or "").strip(),
            "skill_name": str(self._nanobot_config.skill_name or "").strip(),
            "local_mode": True,
            "robot_mode": True,
            "timeout_sec": float(self._nanobot_config.timeout_sec),
        }
        self._last_turn = AgentTurnRecord(backend=self._agent_backend, updated_at=time.time())

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
        enabled = bool(self._nanobot_config.enabled)
        timeout_sec = float(self._nanobot_config.timeout_sec)
        skill_name = str(self._nanobot_config.skill_name or "").strip()
        thinking = str(self._nanobot_config.reasoning_effort or "").strip()
        if not enabled:
            raise MomoRobotError("AGENT_DISABLED", "Nanobot backend is disabled", 503)
        if self._client is None:
            self._client = build_agent_client(
                self._config,
                tool_requester=self._tool_requester,
                tool_request_timeout_sec=self._tool_request_timeout_sec,
            )
        self._resolved_status_fields = {
            "thinking": thinking,
            "skill_name": skill_name,
            "local_mode": True,
            "robot_mode": True,
            "timeout_sec": timeout_sec,
        }
        return self._client

    def _turn_payload_locked(self) -> dict[str, Any]:
        return asdict(self._last_turn)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            client = self._client
            session_id = str(client.session_id).strip() if client is not None else ""
            agent_session_key = str(client.agent_session_key).strip() if client is not None else ""
            resolved = getattr(self, "_resolved_status_fields", {})
            return {
                "backend": self._agent_backend,
                "enabled": bool(self._nanobot_config.enabled),
                "busy": bool(self._busy),
                "thinking": str(resolved.get("thinking", "") or "").strip(),
                "skill_name": str(resolved.get("skill_name", "") or "").strip(),
                "local_mode": bool(resolved.get("local_mode", False)),
                "robot_mode": bool(resolved.get("robot_mode", True)),
                "timeout_sec": float(resolved.get("timeout_sec", 0.0) or 0.0),
                "session_id": session_id,
                "agent_session_key": agent_session_key,
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
            raise MomoRobotError("INVALID_ARGUMENT", "Agent prompt is empty", 400)

        client = None
        started = time.perf_counter()
        with self._lock:
            if self._busy:
                raise MomoRobotError("AGENT_BUSY", "Agent is already processing another turn", 409)
            self._busy = True

        try:
            with self._lock:
                client = self._ensure_client_locked()
            reply = client.ask(message)
            elapsed = time.perf_counter() - started
            turn = AgentTurnRecord(
                backend=self._agent_backend,
                kind=str(kind or "ask"),
                status="ok",
                prompt=message,
                reply=str(reply.text or "").strip(),
                error="",
                session_id=str(reply.session_id or "").strip(),
                agent_session_key=str(client.agent_session_key or "").strip(),
                agent_elapsed_sec=float(elapsed),
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
        except MomoRobotError:
            with self._lock:
                self._busy = False
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            with self._lock:
                self._last_error = str(exc).strip() or "Agent turn failed"
                session_id = str(getattr(client, "session_id", "") or "").strip() if client is not None else ""
                agent_session_key = (
                    str(getattr(client, "agent_session_key", "") or "").strip()
                    if client is not None
                    else ""
                )
                self._last_turn = AgentTurnRecord(
                    backend=self._agent_backend,
                    kind=str(kind or "ask"),
                    status="error",
                    prompt=message,
                    reply="",
                    error=self._last_error,
                    session_id=session_id,
                    agent_session_key=agent_session_key,
                    agent_elapsed_sec=float(elapsed),
                    tts={"requested": False},
                    updated_at=time.time(),
                )
                self._busy = False
            raise MomoRobotError("AGENT_FAILED", self._last_error, 500) from exc

    def ask(self, message: str) -> dict[str, Any]:
        return self._run_turn(kind="ask", prompt=message)

    def warmup(self, prompt: str = "请只回复“就绪”。") -> dict[str, Any]:
        return self._run_turn(kind="warmup", prompt=prompt)

    def reset_session(self) -> dict[str, Any]:
        with self._lock:
            if self._busy:
                raise MomoRobotError("AGENT_BUSY", "Agent is already processing another turn", 409)
            client = self._ensure_client_locked()
            client.reset_session()
            self._last_error = ""
            self._last_turn = AgentTurnRecord(
                backend=self._agent_backend,
                kind="reset_session",
                status="ok",
                prompt="",
                reply="",
                error="",
                session_id=str(client.session_id or "").strip(),
                agent_session_key=str(client.agent_session_key or "").strip(),
                agent_elapsed_sec=0.0,
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
