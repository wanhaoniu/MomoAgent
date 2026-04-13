from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .config import (
    MOMO_AGENT_RUNTIME_DIR,
    OPENCLAW_THINKING_DEFAULT,
    REPO_ROOT,
    SDK_SRC,
    OpenClawConfig,
)

SESSION_STATE_PATH = MOMO_AGENT_RUNTIME_DIR / "openclaw_session_state.json"


def _parse_json_with_noise(raw: str) -> Optional[Any]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        try:
            return json.loads(text[first_obj : last_obj + 1])
        except Exception:
            pass

    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        try:
            return json.loads(text[first_arr : last_arr + 1])
        except Exception:
            pass
    return None


def _extract_text_from_openclaw_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        texts = [_extract_text_from_openclaw_payload(item) for item in payload]
        return "\n".join(text for text in texts if text).strip()
    if isinstance(payload, dict):
        if isinstance(payload.get("payloads"), list):
            texts = []
            for item in payload.get("payloads", []):
                if isinstance(item, dict):
                    text = str(item.get("text", "")).strip()
                    if text:
                        texts.append(text)
            if texts:
                return texts[-1].strip()
        for key in ("text", "message", "content", "reply", "answer"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("data", "result", "output", "choices"):
            if key in payload:
                nested = _extract_text_from_openclaw_payload(payload[key])
                if nested:
                    return nested
    return ""


def _extract_openclaw_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        agent_meta = meta.get("agentMeta")
        if isinstance(agent_meta, dict):
            session_id = str(agent_meta.get("sessionId", "")).strip()
            if session_id:
                return session_id
    for key in ("result", "data", "payload", "output"):
        nested = _extract_openclaw_session_id(payload.get(key))
        if nested:
            return nested
    return ""


def _looks_like_node_request(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    cn_hits = ("节点", "发布一个节点", "哪个节点", "node agent", "nodes")
    en_hits = ("which node", "need a node", "specify a node", "publish a node")
    return any(hit in raw for hit in cn_hits) or any(hit in raw for hit in en_hits)


def _looks_like_python_missing(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    hits = ("python command is not found", "command not found", "`python`", "python: not found")
    return any(hit in raw for hit in hits)


def _looks_like_dispatch_usage_error(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    hits = (
        "not directly supported by the script",
        "use the `call` subcommand",
        "use the call subcommand",
        "can't be found",
    )
    return any(hit in raw for hit in hits)


def _sanitize_openclaw_reply(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "你在执行机械臂控制模式" not in raw:
        return raw
    for marker in ("机械臂已", "夹爪已", "执行成功", "执行失败", "SKILL_NOT_AVAILABLE", "失败", "成功"):
        index = raw.rfind(marker)
        if index >= 0:
            cleaned = raw[index:].strip()
            if cleaned:
                return cleaned
    return raw


@dataclass
class OpenClawReply:
    text: str
    session_id: str
    raw_payload: Any


class OpenClawGatewayBridgeClient:
    def __init__(self, config: OpenClawConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen[str]] = None

    def _resolve_node_bin(self) -> str:
        if self._config.node_bin:
            return self._config.node_bin
        return shutil.which("node") or shutil.which("nodejs") or ""

    def _resolve_script_path(self) -> Path:
        return Path(self._config.gateway_bridge_script).expanduser().resolve()

    def available(self) -> bool:
        return (
            bool(self._config.gateway_bridge_enabled)
            and bool(self._resolve_node_bin())
            and self._resolve_script_path().is_file()
        )

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def _ensure_proc_locked(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        self._stop_locked()
        node_bin = self._resolve_node_bin()
        script_path = self._resolve_script_path()
        if not node_bin:
            raise RuntimeError("Node.js 不可用，无法启动 OpenClaw Gateway bridge")
        if not script_path.is_file():
            raise RuntimeError(f"OpenClaw Gateway bridge 不存在: {script_path}")

        env = os.environ.copy()
        env.setdefault("NODE_NO_WARNINGS", "1")
        self._proc = subprocess.Popen(
            [node_bin, str(script_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=env,
        )
        return self._proc

    def _readline_locked(self, timeout_sec: float) -> str:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise RuntimeError("OpenClaw Gateway bridge 尚未启动")
        ready, _, _ = select.select([proc.stdout], [], [], max(0.1, float(timeout_sec)))
        if not ready:
            raise RuntimeError(f"OpenClaw Gateway bridge 超时 ({timeout_sec:.1f}s)")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("OpenClaw Gateway bridge 已退出")
        return line

    def _read_json_message_locked(self, timeout_sec: float) -> Dict[str, Any]:
        deadline = time.monotonic() + max(1.0, float(timeout_sec))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("OpenClaw Gateway bridge 等待 JSON 响应超时")
            raw = self._readline_locked(remaining)
            stripped = str(raw or "").strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                raise RuntimeError("OpenClaw Gateway bridge 返回了非 JSON 对象")
            return message

    def request(self, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        with self._lock:
            last_exc: Optional[Exception] = None
            for _ in range(2):
                proc = self._ensure_proc_locked()
                if proc.stdin is None:
                    self._stop_locked()
                    raise RuntimeError("OpenClaw Gateway bridge stdin 不可用")
                try:
                    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    message = self._read_json_message_locked(timeout_sec)
                    if str(message.get("id", "")) != str(payload.get("id", "")):
                        raise RuntimeError("OpenClaw Gateway bridge 返回了不匹配的请求 ID")
                    if not bool(message.get("ok")):
                        err = str(message.get("error", "")).strip() or "OpenClaw Gateway bridge 请求失败"
                        raise RuntimeError(err)
                    body = message.get("payload")
                    if not isinstance(body, dict):
                        raise RuntimeError("OpenClaw Gateway bridge payload 格式无效")
                    return body
                except Exception as exc:
                    last_exc = exc
                    self._stop_locked()
            raise RuntimeError(f"OpenClaw Gateway bridge 调用失败: {last_exc}")


class OpenClawClient:
    def __init__(self, config: OpenClawConfig) -> None:
        self._config = config
        self._bridge = OpenClawGatewayBridgeClient(config)
        persisted = None if config.force_new_session else self._load_persisted_state()
        self._session_id = str(config.session_id or "").strip() or str(
            (persisted or {}).get("session_id", "")
        ).strip()
        self._bridge_session_key = str((persisted or {}).get("bridge_session_key", "")).strip()
        if not self._bridge_session_key:
            session_key_suffix = self._session_id or uuid.uuid4().hex[:8]
            self._bridge_session_key = f"agent:{self._config.agent_id}:{session_key_suffix}"

    def close(self) -> None:
        self._bridge.close()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def bridge_session_key(self) -> str:
        return self._bridge_session_key

    def _state_identity(self) -> Dict[str, str]:
        return {
            "agent_id": str(self._config.agent_id or "").strip(),
            "skill_name": str(self._config.skill_name or "").strip(),
            "local_mode": "1" if self._config.local_mode else "0",
        }

    def _load_persisted_state(self) -> Optional[Dict[str, Any]]:
        try:
            if not SESSION_STATE_PATH.is_file():
                return None
            payload = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            return None
        if identity != self._state_identity():
            return None
        return payload

    def _persist_state(self) -> None:
        MOMO_AGENT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "identity": self._state_identity(),
            "session_id": self._session_id,
            "bridge_session_key": self._bridge_session_key,
            "updated_at": time.time(),
        }
        SESSION_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reset_session(self) -> None:
        self._session_id = ""
        self._bridge_session_key = f"agent:{self._config.agent_id}:{uuid.uuid4().hex[:8]}"
        try:
            if SESSION_STATE_PATH.exists():
                SESSION_STATE_PATH.unlink()
        except Exception:
            pass

    def _build_subprocess_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        if SDK_SRC.exists():
            sdk_src_str = str(SDK_SRC.resolve())
            existing = str(env.get("PYTHONPATH", "")).strip()
            env["PYTHONPATH"] = f"{sdk_src_str}:{existing}" if existing else sdk_src_str
        return env

    def _build_agent_cmd(self, message: str, session_id: str) -> list[str]:
        cmd = [
            self._config.binary,
            "--no-color",
            "agent",
            "--json",
            "--message",
            str(message or ""),
            "--thinking",
            self._config.thinking or OPENCLAW_THINKING_DEFAULT,
        ]
        if self._config.local_mode:
            cmd.append("--local")
        if session_id:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--agent", self._config.agent_id])
        return cmd

    def _build_bridge_payload(self, message: str) -> Dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "op": "agent_turn",
            "message": str(message or ""),
            "agent_id": self._config.agent_id,
            "thinking": self._config.thinking or OPENCLAW_THINKING_DEFAULT,
            "timeout_sec": self._config.timeout_sec,
            "session_key": self._bridge_session_key,
        }

    def _invoke_bridge_once(self, message: str) -> Dict[str, Any]:
        result = self._bridge.request(
            self._build_bridge_payload(message),
            timeout_sec=self._config.timeout_sec + 5.0,
        )
        reply = str(result.get("reply", "")).strip()
        session_id = str(result.get("session_id", "")).strip()
        payload = {
            "text": reply,
            "meta": {"agentMeta": {"sessionId": session_id}},
            "result": {"meta": {"agentMeta": {"sessionId": session_id}}},
            "bridge": {
                "runId": str(result.get("run_id", "")).strip(),
                "sessionKey": str(result.get("session_key", "")).strip(),
                "timing": result.get("timing", {}),
            },
        }
        return {"payload": payload, "stdout": "", "stderr": ""}

    def _invoke_cli_once(self, message: str, session_id: str) -> Dict[str, Any]:
        cmd = self._build_agent_cmd(message, session_id)
        cwd = None
        if self._config.robot_mode:
            skill_dir = Path.home() / ".openclaw" / "skills" / self._config.skill_name
            if skill_dir.is_dir():
                cwd = str(skill_dir)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._config.timeout_sec,
            check=False,
            cwd=cwd,
            env=self._build_subprocess_env(),
        )
        stdout_text = str(proc.stdout or "").strip()
        stderr_text = str(proc.stderr or "").strip()
        if proc.returncode != 0:
            err = stderr_text or stdout_text or f"OpenClaw 返回错误码 {proc.returncode}"
            raise RuntimeError(err)
        payload = _parse_json_with_noise(stdout_text)
        if payload is None and stderr_text:
            payload = _parse_json_with_noise(stderr_text)
        if payload is None:
            payload = {"text": stdout_text}
        return {"payload": payload, "stdout": stdout_text, "stderr": stderr_text}

    def ask(self, message: str) -> OpenClawReply:
        if not self._config.enabled:
            raise RuntimeError("OpenClaw 已被禁用")
        text = str(message or "").strip()
        if not text:
            raise RuntimeError("OpenClaw 输入为空")

        attempts = 1 + max(0, int(self._config.node_retry_count))
        current_session = "" if self._config.force_new_session else self._session_id

        for attempt_index in range(attempts):
            if self._bridge.available() and not self._config.local_mode:
                result = self._invoke_bridge_once(text)
            else:
                result = self._invoke_cli_once(text, current_session)

            payload = result.get("payload")
            stdout_text = str(result.get("stdout", "")).strip()
            current_session = _extract_openclaw_session_id(payload) or current_session

            reply = _extract_text_from_openclaw_payload(payload) or stdout_text
            reply = str(reply or "").strip()
            if not reply:
                raise RuntimeError("OpenClaw 未返回可用文本")

            if _looks_like_node_request(reply) and attempt_index + 1 < attempts:
                continue
            if _looks_like_dispatch_usage_error(reply) and attempt_index + 1 < attempts:
                continue
            if _looks_like_node_request(reply):
                raise RuntimeError(
                    "OpenClaw 仍在请求节点，未进入 soarmmoce-control 技能执行链路。"
                    "请检查 ~/.openclaw/skills 中技能安装状态。"
                )
            if _looks_like_dispatch_usage_error(reply):
                raise RuntimeError(
                    "OpenClaw 已进入技能链路，但工具脚本调用格式不正确。"
                    "应使用 soarmmoce-control 技能里定义的标准入口。"
                )
            if _looks_like_python_missing(reply):
                raise RuntimeError(
                    "OpenClaw 已进入技能链路，但执行环境缺少 `python` 命令。"
                    "请在技能入口里固定使用 python3。"
                )

            if not self._config.force_new_session:
                self._session_id = current_session
                self._persist_state()
            return OpenClawReply(
                text=_sanitize_openclaw_reply(reply),
                session_id=current_session,
                raw_payload=payload,
            )

        raise RuntimeError("OpenClaw 未返回可用回复")


_OPENCLAW_CLIENTS: list[OpenClawClient] = []


def build_openclaw_client(config: OpenClawConfig) -> OpenClawClient:
    client = OpenClawClient(config)
    _OPENCLAW_CLIENTS.append(client)
    return client


def _close_all_clients() -> None:
    while _OPENCLAW_CLIENTS:
        client = _OPENCLAW_CLIENTS.pop()
        try:
            client.close()
        except Exception:
            pass


atexit.register(_close_all_clients)
