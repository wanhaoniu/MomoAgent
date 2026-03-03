"""Floating speech input window with recording + Groq STT loop."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import threading
import wave
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests
import sounddevice as sd
from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget

try:
    from .skills_dispatcher import LocalToolDispatcher, OPENAI_TOOL_SCHEMAS, ensure_tools_schema_file
except Exception:
    from skills_dispatcher import LocalToolDispatcher, OPENAI_TOOL_SCHEMAS, ensure_tools_schema_file

# Extracted for easy replacement with env var later.
# Recommended: export GROQ_API_KEY and remove fallback literal.
GROQ_API_KEY_FALLBACK = " "
GROQ_STT_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL_DEFAULT = "whisper-large-v3"

OPENCLAW_BIN_DEFAULT = "openclaw"
OPENCLAW_AGENT_ID_DEFAULT = "main"
OPENCLAW_TIMEOUT_SEC_DEFAULT = 90.0

OPENCLAW_THINKING_DEFAULT = "minimal"
OPENCLAW_MAX_TOOL_TURNS_DEFAULT = 4
OPENCLAW_TOOL_REQUEST_TIMEOUT_SEC_DEFAULT = 6.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _extract_text_from_stt_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        texts = [_extract_text_from_stt_payload(item) for item in payload]
        texts = [x for x in texts if x]
        return " ".join(texts).strip()
    if isinstance(payload, dict):
        for key in ("text", "transcript", "asr_text", "recognized_text", "result_text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("result", "data", "output", "choices", "results"):
            if key in payload:
                got = _extract_text_from_stt_payload(payload[key])
                if got:
                    return got
    return ""


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
        snippet = text[first_obj : last_obj + 1]
        try:
            return json.loads(snippet)
        except Exception:
            pass

    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        snippet = text[first_arr : last_arr + 1]
        try:
            return json.loads(snippet)
        except Exception:
            pass
    return None


def _extract_text_from_openclaw_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        texts = [_extract_text_from_openclaw_payload(x) for x in payload]
        texts = [x for x in texts if x]
        return "\n".join(texts).strip()
    if isinstance(payload, dict):
        if isinstance(payload.get("payloads"), list):
            texts: List[str] = []
            for item in payload.get("payloads", []):
                if isinstance(item, dict):
                    t = str(item.get("text", "")).strip()
                    if t:
                        texts.append(t)
            if texts:
                return "\n".join(texts).strip()
        for key in ("text", "message", "content", "reply", "answer"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key in ("data", "result", "output", "choices"):
            if key in payload:
                got = _extract_text_from_openclaw_payload(payload[key])
                if got:
                    return got
    return ""


def _extract_openclaw_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return ""
    agent_meta = meta.get("agentMeta")
    if not isinstance(agent_meta, dict):
        return ""
    sid = agent_meta.get("sessionId")
    return str(sid).strip() if sid is not None else ""


def _parse_tool_arguments(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_tool_call(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    function_part = raw.get("function")
    name = ""
    args_val: Any = {}
    if isinstance(function_part, dict):
        name = str(function_part.get("name", "")).strip()
        args_val = function_part.get("arguments", {})
    if not name:
        name = str(raw.get("name", "")).strip()
    if function_part is None:
        args_val = raw.get("arguments", raw.get("args", raw.get("parameters", {})))
    if not name:
        return None
    call_id = str(raw.get("id", raw.get("tool_call_id", ""))).strip()
    return {
        "id": call_id,
        "name": name,
        "arguments": _parse_tool_arguments(args_val),
    }


def _extract_tool_calls_from_payload(payload: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    seen = set()

    def _extract_json_candidates_from_text(text: str, max_items: int = 24) -> List[Any]:
        s = str(text or "").strip()
        if not s:
            return []
        out: List[Any] = []

        def _try_add(raw: str):
            raw_norm = str(raw or "").strip()
            if not raw_norm:
                return
            try:
                parsed = json.loads(raw_norm)
            except Exception:
                return
            if isinstance(parsed, (dict, list)):
                out.append(parsed)

        # Whole text as JSON
        _try_add(s)

        # JSON code blocks
        for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", s, flags=re.IGNORECASE):
            _try_add(m.group(1))

        # Raw JSON fragments embedded in natural language
        dec = json.JSONDecoder()
        i = 0
        n = len(s)
        while i < n and len(out) < max_items:
            ch = s[i]
            if ch not in "{[":
                i += 1
                continue
            try:
                parsed, end = dec.raw_decode(s[i:])
            except Exception:
                i += 1
                continue
            if isinstance(parsed, (dict, list)):
                out.append(parsed)
            i += max(1, end)
        return out

    def _push_call(candidate: Any):
        got = _normalize_tool_call(candidate)
        if not got:
            return
        key = (
            got.get("id", ""),
            got.get("name", ""),
            json.dumps(got.get("arguments", {}), ensure_ascii=False, sort_keys=True),
        )
        if key in seen:
            return
        seen.add(key)
        calls.append(got)

    def _scan(node: Any):
        if isinstance(node, dict):
            if isinstance(node.get("tool_calls"), list):
                for item in node.get("tool_calls", []):
                    _push_call(item)
            if isinstance(node.get("function_call"), dict):
                _push_call(node.get("function_call"))
            if isinstance(node.get("function_calls"), list):
                for item in node.get("function_calls", []):
                    _push_call(item)
            # Common OpenAI-like response nesting.
            if isinstance(node.get("message"), dict):
                _scan(node.get("message"))
            elif isinstance(node.get("message"), str):
                _scan(node.get("message"))
            if isinstance(node.get("delta"), dict):
                _scan(node.get("delta"))
            elif isinstance(node.get("delta"), str):
                _scan(node.get("delta"))
            for key in ("text", "content", "reply", "answer"):
                if isinstance(node.get(key), str):
                    _scan(node.get(key))
            for key in ("choices", "payloads", "data", "result", "output"):
                if isinstance(node.get(key), list):
                    for item in node.get(key, []):
                        _scan(item)
                elif isinstance(node.get(key), dict):
                    _scan(node.get(key))
        elif isinstance(node, list):
            for item in node:
                _scan(item)
        elif isinstance(node, str):
            for candidate in _extract_json_candidates_from_text(node):
                _scan(candidate)

    _scan(payload)
    return calls


def groq_audio_to_text(
    wav_bytes: bytes,
    api_key: str,
    url: str,
    model: str,
    language: str = "zh",
    timeout_sec: float = 45.0,
) -> str:
    if not wav_bytes:
        raise ValueError("Empty audio data")
    if not api_key:
        raise ValueError("Groq API key is empty")

    headers = {"Authorization": f"Bearer {api_key}"}
    data: Dict[str, str] = {}
    if model:
        data["model"] = model
    if language:
        data["language"] = language

    data["response_format"] = "json"

    response = requests.post(
        url,
        headers=headers,
        data=data,
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        timeout=float(timeout_sec),
    )
    response.raise_for_status()

    try:
        payload: Any = response.json()
    except Exception:
        text_raw = response.text.strip()
        if text_raw:
            return text_raw
        raise RuntimeError("Groq STT response is empty")

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            err_msg = str(err.get("message", "Groq STT failed"))
            raise RuntimeError(err_msg)

    text = _extract_text_from_stt_payload(payload)
    if not text:
        raise RuntimeError(f"Groq STT response has no text: {payload}")
    return text


class _GroqSttWorker(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, wav_bytes: bytes, api_key: str, url: str, model: str):
        super().__init__()
        self._wav_bytes = wav_bytes
        self._api_key = api_key
        self._url = url
        self._model = model

    def run(self):
        try:
            text = groq_audio_to_text(
                wav_bytes=self._wav_bytes,
                api_key=self._api_key,
                url=self._url,
                model=self._model,
                language="zh",
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(text)


class _OpenClawAgentWorker(QThread):
    done = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    action_started = pyqtSignal(str)
    sig_tool_request = pyqtSignal(str, dict, str)  # tool_name, payload, request_id
    sig_tool_result = pyqtSignal(str, bool, dict)  # request_id, ok, result

    def __init__(
        self,
        message: str,
        openclaw_bin: str,
        agent_id: str,
        session_id: str,
        local_mode: bool,
        timeout_sec: float,
        thinking: str,
        max_tool_turns: int = OPENCLAW_MAX_TOOL_TURNS_DEFAULT,
        tools_schema_path: Optional[str] = None,
        tool_request_timeout_sec: float = OPENCLAW_TOOL_REQUEST_TIMEOUT_SEC_DEFAULT,
    ):
        super().__init__()
        self._message = str(message or "").strip()
        self._openclaw_bin = str(openclaw_bin or OPENCLAW_BIN_DEFAULT).strip() or OPENCLAW_BIN_DEFAULT
        self._agent_id = str(agent_id or OPENCLAW_AGENT_ID_DEFAULT).strip() or OPENCLAW_AGENT_ID_DEFAULT
        self._session_id = str(session_id or "").strip()
        self._local_mode = bool(local_mode)
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._thinking = str(thinking or OPENCLAW_THINKING_DEFAULT).strip() or OPENCLAW_THINKING_DEFAULT
        self._max_tool_turns = max(1, int(max_tool_turns))
        self._tools_schema_path = str(tools_schema_path or "").strip()
        self._tool_request_timeout_sec = max(2.0, float(tool_request_timeout_sec))
        self._dispatcher = LocalToolDispatcher(
            tool_requester=self._request_tool_on_main_thread,
            tool_request_timeout_sec=self._tool_request_timeout_sec,
        )
        self._has_tools_arg_cache: Optional[bool] = None
        self._pending_tool_results: Dict[str, queue.Queue] = {}
        self._pending_tool_lock = threading.Lock()
        self.sig_tool_result.connect(self._on_sig_tool_result)

    def _request_tool_on_main_thread(
        self,
        tool_name: str,
        payload: Dict[str, Any],
        request_id: str,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        req_id = str(request_id or "").strip()
        if not req_id:
            req_id = f"req_{time.time_ns()}"
        wait_timeout = max(0.5, float(timeout_sec))

        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_tool_lock:
            self._pending_tool_results[req_id] = q

        try:
            self.sig_tool_request.emit(str(tool_name or ""), dict(payload or {}), req_id)
            try:
                got_ok, got_result = q.get(timeout=wait_timeout)
            except queue.Empty:
                return {
                    "ok": False,
                    "result": {
                        "ok": False,
                        "error": f"tool result timeout ({wait_timeout:.1f}s)",
                        "tool": str(tool_name or ""),
                    },
                }
            result_payload = got_result if isinstance(got_result, dict) else {"value": got_result}
            return {"ok": bool(got_ok), "result": result_payload}
        finally:
            with self._pending_tool_lock:
                self._pending_tool_results.pop(req_id, None)

    def _on_sig_tool_result(self, request_id: str, ok: bool, result: Dict[str, Any]):
        req_id = str(request_id or "").strip()
        if not req_id:
            return
        with self._pending_tool_lock:
            q = self._pending_tool_results.get(req_id)
        if q is None:
            return
        payload = result if isinstance(result, dict) else {"value": result}
        try:
            q.put_nowait((bool(ok), payload))
        except Exception:
            pass

    def _supports_tools_arg(self) -> bool:
        if self._has_tools_arg_cache is not None:
            return bool(self._has_tools_arg_cache)
        try:
            proc = subprocess.run(
                [self._openclaw_bin, "agent", "--help"],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            output = f"{proc.stdout}\n{proc.stderr}"
            self._has_tools_arg_cache = "--tools" in output
        except Exception:
            self._has_tools_arg_cache = False
        return bool(self._has_tools_arg_cache)

    def _build_agent_cmd(self, message: str, session_id: str) -> List[str]:
        cmd = [
            self._openclaw_bin,
            "--no-color",
            "agent",
            "--json",
            "--message",
            str(message or ""),
            "--thinking",
            self._thinking,
        ]
        if self._local_mode:
            cmd.append("--local")

        if session_id:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--agent", self._agent_id])

        if self._supports_tools_arg():
            schema_path = ensure_tools_schema_file(Path(self._tools_schema_path) if self._tools_schema_path else None)
            cmd.extend(["--tools", str(schema_path)])
        return cmd

    def _invoke_openclaw_once(self, message: str, session_id: str) -> Dict[str, Any]:
        cmd = self._build_agent_cmd(message=message, session_id=session_id)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError(f"找不到 OpenClaw 可执行文件: {self._openclaw_bin}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("OpenClaw 调用超时")
        except Exception as exc:
            raise RuntimeError(f"OpenClaw 调用失败: {exc}") from exc

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
        return {
            "payload": payload,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    @staticmethod
    def _make_tool_result_message(results: List[Dict[str, Any]]) -> str:
        blob = json.dumps(results, ensure_ascii=False)
        return (
            "以下是你刚请求的工具执行结果（JSON）：\n"
            f"{blob}\n\n"
            "请基于这些结果继续，并严格遵守：\n"
            "1) 必须优先依据每条 result 里的 ok / within_tolerance 字段判断执行是否成功。\n"
            "2) 若 ok=true（或 within_tolerance=true），禁止描述为“误差较大”或“失败”。\n"
            "3) 若 ok=false，才可以描述失败或误差超限。\n"
            "4) 如果当前目标复杂（如抓取、跳舞），优先尝试 run_skill，而不是直接拒绝。\n"
            "5) 如果还需要工具调用，请继续返回 tool_calls；如果任务完成，请直接返回最终中文答复。"
        )

    @staticmethod
    def _make_tool_instruction_prefix(user_message: str) -> str:
        schema_blob = json.dumps(OPENAI_TOOL_SCHEMAS, ensure_ascii=False)
        return (
            "你正在控制 SO-ARM-MoceArm。本轮可用工具如下（OpenAI tools schema）：\n"
            f"{schema_blob}\n\n"
            "规则：\n"
            "1) 当你需要执行动作时，请只返回 JSON，不要输出解释、思考过程或多余文本。\n"
            "2) JSON 格式必须是："
            "{\"tool_calls\":[{\"id\":\"call_x\",\"type\":\"function\",\"function\":{\"name\":\"...\",\"arguments\":\"{...}\"}}]}。\n"
            "3) 当用户要求执行相对移动（如“高一点”“往左移动”）时，必须先调用 get_robot_state 获取当前 (x,y,z) 绝对坐标，"
            "再把相对偏移量加到当前坐标上，最后调用 move_robot_arm。\n"
            "4) 遇到高层目标（如“抓红苹果”“让机械臂跳舞”）时，优先调用 run_skill：\n"
            "   - 抓苹果 -> run_skill(name=\"grasp_apple_mock\", params={...})\n"
            "   - 跳舞 -> run_skill(name=\"dance_short\", params={...})\n"
            "   只有明确超出当前能力且存在安全风险时，才简短拒绝。\n"
            "5) 对无效语音或闲聊（非控制指令）返回简短自然回复，不要生硬报错。\n\n"
            f"用户请求：{user_message}"
        )

    def run(self):
        if not self._message:
            self.failed.emit("OpenClaw 输入为空")
            return

        current_session = self._session_id
        if self._supports_tools_arg():
            current_message = self._message
        else:
            current_message = self._make_tool_instruction_prefix(self._message)
        tool_turn = 0

        while True:
            if self.isInterruptionRequested():
                self.failed.emit("OpenClaw 请求已取消")
                return

            try:
                result = self._invoke_openclaw_once(message=current_message, session_id=current_session)
            except Exception as exc:
                self.failed.emit(str(exc))
                return

            payload = result.get("payload")
            stdout_text = str(result.get("stdout", "")).strip()
            next_session = _extract_openclaw_session_id(payload)
            if next_session:
                current_session = next_session

            tool_calls = _extract_tool_calls_from_payload(payload)
            if tool_calls:
                tool_turn += 1
                if tool_turn > self._max_tool_turns:
                    self.failed.emit("Tool 调用轮数超限，已中止")
                    return

                call_results: List[Dict[str, Any]] = []
                for call in tool_calls:
                    name = str(call.get("name", "")).strip()
                    args = call.get("arguments", {})
                    if not isinstance(args, dict):
                        args = {}
                    self.action_started.emit(name)
                    try:
                        tool_result = self._dispatcher.dispatch(name=name, arguments=args)
                        ok = True
                    except Exception as exc:
                        tool_result = f"tool execution failed: {exc}"
                        ok = False
                    call_results.append(
                        {
                            "tool_call_id": str(call.get("id", "")).strip(),
                            "name": name,
                            "arguments": args,
                            "ok": ok,
                            "result": tool_result,
                        }
                    )

                current_message = self._make_tool_result_message(call_results)
                continue

            reply = _extract_text_from_openclaw_payload(payload)
            if not reply:
                reply = stdout_text
            reply = str(reply or "").strip()
            if not reply:
                self.failed.emit("OpenClaw 未返回可用文本")
                return
            self.done.emit(reply, current_session)
            return


class SpeechInputWindow(QWidget):
    """Frameless always-on-top speech window with animated ripples."""

    closed = pyqtSignal()
    listening_changed = pyqtSignal(bool)
    transcribing_changed = pyqtSignal(bool)
    transcript_ready = pyqtSignal(str)
    transcript_failed = pyqtSignal(str)
    agent_reply_ready = pyqtSignal(str)
    agent_failed = pyqtSignal(str)
    agent_action_started = pyqtSignal(str)
    agent_session_changed = pyqtSignal(str)
    tool_request = pyqtSignal(str, dict, str)  # tool_name, payload, request_id

    def __init__(self, title: str, icon_path: Optional[Path] = None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(300, 300)

        self._theme = "light"
        self._phase = 0.0
        self._ripple_count = 3
        self._cycle_ms = 1600.0

        self._drag_offset: Optional[QPoint] = None
        self._dragging = False
        self._press_global_pos: Optional[QPoint] = None
        self._icon_pixmap = QPixmap()

        self._is_listening = False
        self._is_transcribing = False
        self._is_agent_running = False
        self._status_text = "点击开始说话"
        self._last_text = ""
        self._last_agent_reply = ""

        self._sample_rate = 16000
        self._channels = 1
        self._audio_stream: Optional[sd.InputStream] = None
        self._audio_chunks: List[np.ndarray] = []
        self._stt_worker: Optional[_GroqSttWorker] = None
        self._openclaw_worker: Optional[_OpenClawAgentWorker] = None

        self._groq_api_key = os.getenv("GROQ_API_KEY", GROQ_API_KEY_FALLBACK).strip()
        self._groq_stt_url = os.getenv("GROQ_STT_URL", GROQ_STT_URL_DEFAULT).strip()
        self._groq_stt_model = os.getenv("GROQ_STT_MODEL", GROQ_STT_MODEL_DEFAULT).strip()

        self._openclaw_enabled = _env_bool("OPENCLAW_ENABLED", True)
        self._openclaw_bin = str(os.getenv("OPENCLAW_BIN", OPENCLAW_BIN_DEFAULT)).strip() or OPENCLAW_BIN_DEFAULT
        self._openclaw_agent_id = str(
            os.getenv("OPENCLAW_AGENT_ID", OPENCLAW_AGENT_ID_DEFAULT)
        ).strip() or OPENCLAW_AGENT_ID_DEFAULT
        self._openclaw_local_mode = _env_bool("OPENCLAW_LOCAL", True)
        self._openclaw_thinking = str(
            os.getenv("OPENCLAW_THINKING", OPENCLAW_THINKING_DEFAULT)
        ).strip() or OPENCLAW_THINKING_DEFAULT
        try:
            self._openclaw_timeout_sec = float(
                str(os.getenv("OPENCLAW_TIMEOUT_SEC", str(OPENCLAW_TIMEOUT_SEC_DEFAULT))).strip()
            )
        except Exception:
            self._openclaw_timeout_sec = OPENCLAW_TIMEOUT_SEC_DEFAULT
        self._openclaw_timeout_sec = max(5.0, self._openclaw_timeout_sec)
        self._openclaw_session_id = str(os.getenv("OPENCLAW_SESSION_ID", "")).strip()
        try:
            self._openclaw_max_tool_turns = int(
                str(os.getenv("OPENCLAW_MAX_TOOL_TURNS", str(OPENCLAW_MAX_TOOL_TURNS_DEFAULT))).strip()
            )
        except Exception:
            self._openclaw_max_tool_turns = OPENCLAW_MAX_TOOL_TURNS_DEFAULT
        self._openclaw_max_tool_turns = max(1, self._openclaw_max_tool_turns)
        self._openclaw_tools_schema_path = str(os.getenv("OPENCLAW_TOOLS_PATH", "")).strip()
        try:
            self._openclaw_tool_request_timeout_sec = float(
                str(
                    os.getenv(
                        "OPENCLAW_TOOL_REQUEST_TIMEOUT_SEC",
                        str(OPENCLAW_TOOL_REQUEST_TIMEOUT_SEC_DEFAULT),
                    )
                ).strip()
            )
        except Exception:
            self._openclaw_tool_request_timeout_sec = OPENCLAW_TOOL_REQUEST_TIMEOUT_SEC_DEFAULT
        self._openclaw_tool_request_timeout_sec = max(2.0, self._openclaw_tool_request_timeout_sec)

        if icon_path is not None:
            self.set_icon(icon_path)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)

    def set_window_title(self, title: str):
        self.setWindowTitle(title)

    def set_theme(self, theme: str):
        self._theme = "dark" if str(theme).strip().lower() == "dark" else "light"
        self.update()

    def set_icon(self, icon_path: Path):
        pixmap = QPixmap(str(icon_path))
        if not pixmap.isNull():
            self._icon_pixmap = pixmap
            self.update()

    def set_groq_config(self, api_key: str, stt_url: Optional[str] = None, model: Optional[str] = None):
        self._groq_api_key = str(api_key or "").strip()
        if stt_url is not None:
            self._groq_stt_url = str(stt_url).strip()
        if model is not None:
            self._groq_stt_model = str(model).strip()

    def set_openclaw_config(
        self,
        enabled: bool = True,
        openclaw_bin: Optional[str] = None,
        agent_id: Optional[str] = None,
        local_mode: Optional[bool] = None,
        thinking: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        session_id: Optional[str] = None,
        max_tool_turns: Optional[int] = None,
        tools_schema_path: Optional[str] = None,
        tool_request_timeout_sec: Optional[float] = None,
    ):
        self._openclaw_enabled = bool(enabled)
        if openclaw_bin is not None:
            val = str(openclaw_bin).strip()
            if val:
                self._openclaw_bin = val
        if agent_id is not None:
            val = str(agent_id).strip()
            if val:
                self._openclaw_agent_id = val
        if local_mode is not None:
            self._openclaw_local_mode = bool(local_mode)
        if thinking is not None:
            val = str(thinking).strip()
            if val:
                self._openclaw_thinking = val
        if timeout_sec is not None:
            try:
                self._openclaw_timeout_sec = max(5.0, float(timeout_sec))
            except Exception:
                pass
        if session_id is not None:
            self._openclaw_session_id = str(session_id).strip()
        if max_tool_turns is not None:
            try:
                self._openclaw_max_tool_turns = max(1, int(max_tool_turns))
            except Exception:
                pass
        if tools_schema_path is not None:
            self._openclaw_tools_schema_path = str(tools_schema_path).strip()
        if tool_request_timeout_sec is not None:
            try:
                self._openclaw_tool_request_timeout_sec = max(2.0, float(tool_request_timeout_sec))
            except Exception:
                pass

    # Backward compatibility for old call sites.
    def set_minimax_config(self, api_key: str, stt_url: Optional[str] = None, model: Optional[str] = None):
        self.set_groq_config(api_key=api_key, stt_url=stt_url, model=model)

    @staticmethod
    def get_openclaw_tool_schemas() -> List[Dict[str, Any]]:
        return list(OPENAI_TOOL_SCHEMAS)

    @staticmethod
    def export_openclaw_tool_schemas(path: Optional[Path] = None) -> Path:
        # Ensures schema can be provided to runtimes that support --tools <json-file>.
        if path is not None:
            return ensure_tools_schema_file(Path(path))
        return ensure_tools_schema_file()

    def _on_anim_tick(self):
        self._phase = (self._phase + self._anim_timer.interval() / self._cycle_ms) % 1.0
        self.update()

    def _set_ripple_active(self, active: bool):
        active = bool(active)
        if active:
            if not self._anim_timer.isActive():
                self._phase = 0.0
                self._anim_timer.start()
        else:
            if self._anim_timer.isActive():
                self._anim_timer.stop()
            self._phase = 0.0
        self.update()

    def _ripple_base_color(self) -> QColor:
        if self._theme == "dark":
            return QColor(168, 201, 255)
        return QColor(132, 167, 236)

    def _center_fill_color(self) -> QColor:
        if self._theme == "dark":
            return QColor(20, 31, 48, 230)
        return QColor(255, 255, 255, 235)

    def _icon_bg_color(self) -> QColor:
        if self._theme == "dark":
            return QColor(236, 243, 255, 235)
        return QColor(255, 255, 255, 245)

    def _status_text_color(self) -> QColor:
        if self._theme == "dark":
            return QColor(217, 225, 238)
        return QColor(42, 53, 72)

    def _audio_callback(self, indata, frames, _time_info, status):
        if status:
            return
        if frames <= 0:
            return
        self._audio_chunks.append(np.array(indata, dtype=np.int16, copy=True))

    def _start_listening(self):
        if self._is_listening or self._is_transcribing:
            return
        try:
            self._audio_chunks = []
            self._audio_stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                callback=self._audio_callback,
                blocksize=0,
            )
            self._audio_stream.start()
        except Exception as exc:
            self._status_text = f"录音启动失败: {exc}"
            self.transcript_failed.emit(self._status_text)
            self.update()
            return

        self._is_listening = True
        self._status_text = "录音中，点击结束"
        self.listening_changed.emit(True)
        self._set_ripple_active(True)

    def _stop_listening(self):
        if not self._is_listening:
            return b""
        self._is_listening = False
        self.listening_changed.emit(False)
        self._set_ripple_active(False)

        if self._audio_stream is not None:
            try:
                self._audio_stream.stop()
            except Exception:
                pass
            try:
                self._audio_stream.close()
            except Exception:
                pass
            self._audio_stream = None

        if not self._audio_chunks:
            self._status_text = "未检测到语音输入"
            self.update()
            return b""

        audio = np.concatenate(self._audio_chunks, axis=0)
        self._audio_chunks = []
        with io.BytesIO() as buf:
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(self._channels)
                wf.setsampwidth(2)
                wf.setframerate(self._sample_rate)
                wf.writeframes(audio.tobytes())
            wav_bytes = buf.getvalue()
        return wav_bytes

    def _start_stt(self, wav_bytes: bytes):
        if not wav_bytes:
            self._status_text = "未检测到语音输入"
            self.update()
            return
        if self._is_transcribing:
            return

        self._is_transcribing = True
        self.transcribing_changed.emit(True)
        self._status_text = "识别中..."
        self.update()

        self._stt_worker = _GroqSttWorker(
            wav_bytes=wav_bytes,
            api_key=self._groq_api_key,
            url=self._groq_stt_url,
            model=self._groq_stt_model,
        )
        self._stt_worker.done.connect(self._on_stt_done)
        self._stt_worker.failed.connect(self._on_stt_failed)
        self._stt_worker.finished.connect(self._on_stt_finished)
        self._stt_worker.start()

    def _start_openclaw(self, prompt_text: str):
        text = str(prompt_text or "").strip()
        if not text:
            return
        if not self._openclaw_enabled:
            return
        if self._is_agent_running:
            self._status_text = "OpenClaw 正在处理中，请稍候..."
            self.update()
            return

        self._is_agent_running = True
        self._status_text = f"你说: {text}\nOpenClaw处理中..."
        self.update()

        self._openclaw_worker = _OpenClawAgentWorker(
            message=text,
            openclaw_bin=self._openclaw_bin,
            agent_id=self._openclaw_agent_id,
            session_id=self._openclaw_session_id,
            local_mode=self._openclaw_local_mode,
            timeout_sec=self._openclaw_timeout_sec,
            thinking=self._openclaw_thinking,
            max_tool_turns=self._openclaw_max_tool_turns,
            tools_schema_path=self._openclaw_tools_schema_path,
            tool_request_timeout_sec=self._openclaw_tool_request_timeout_sec,
        )
        self._openclaw_worker.done.connect(self._on_openclaw_done)
        self._openclaw_worker.failed.connect(self._on_openclaw_failed)
        self._openclaw_worker.action_started.connect(self._on_openclaw_action_started)
        self._openclaw_worker.sig_tool_request.connect(self._on_openclaw_tool_request)
        self._openclaw_worker.finished.connect(self._on_openclaw_finished)
        self._openclaw_worker.start()

    def _on_openclaw_tool_request(self, tool_name: str, payload: Dict[str, Any], request_id: str):
        self.tool_request.emit(str(tool_name or ""), dict(payload or {}), str(request_id or ""))

    def submit_tool_result(self, request_id: str, ok: bool, result: Dict[str, Any]):
        if self._openclaw_worker is None:
            return
        req_id = str(request_id or "").strip()
        if not req_id:
            return
        payload = result if isinstance(result, dict) else {"value": result}
        self._openclaw_worker.sig_tool_result.emit(req_id, bool(ok), payload)

    def _on_openclaw_action_started(self, action_name: str):
        action = str(action_name or "").strip() or "unknown"
        self._status_text = f"正在执行动作: {action}..."
        self.agent_action_started.emit(action)
        self.update()

    def _on_openclaw_done(self, reply: str, session_id: str):
        reply_text = str(reply or "").strip()
        self._last_agent_reply = reply_text
        if reply_text:
            self._status_text = f"Momo: {reply_text}"
            self.agent_reply_ready.emit(reply_text)
        else:
            self._status_text = "OpenClaw 未返回文本"
            self.agent_failed.emit(self._status_text)

        sid = str(session_id or "").strip()
        if sid and sid != self._openclaw_session_id:
            self._openclaw_session_id = sid
            self.agent_session_changed.emit(sid)
        self.update()

    def _on_openclaw_failed(self, error_text: str):
        msg = str(error_text or "").strip() or "OpenClaw 调用失败"
        self._status_text = msg
        self.agent_failed.emit(msg)
        self.update()

    def _on_openclaw_finished(self):
        self._is_agent_running = False
        self._openclaw_worker = None
        self.update()

    def _on_stt_done(self, text: str):
        self._last_text = str(text).strip()
        self.transcript_ready.emit(self._last_text)
        if not self._last_text:
            self._status_text = "未识别到有效文本"
            self.update()
            return

        if self._openclaw_enabled:
            self._start_openclaw(self._last_text)
        else:
            self._status_text = self._last_text
        self.update()

    def _on_stt_failed(self, error_text: str):
        msg = str(error_text).strip() or "语音识别失败"
        self._status_text = msg
        self.transcript_failed.emit(msg)
        self.update()

    def _on_stt_finished(self):
        self._is_transcribing = False
        self.transcribing_changed.emit(False)
        self._stt_worker = None
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = self.rect()
        center = QPointF(rect.center().x(), rect.center().y() - 18)
        side = float(min(rect.width(), rect.height()))
        min_radius = side * 0.19
        max_radius = side * 0.47
        base = self._ripple_base_color()

        painter.setPen(Qt.NoPen)
        painter.setBrush(self._center_fill_color())
        painter.drawEllipse(center, min_radius * 1.22, min_radius * 1.22)

        if self._is_listening:
            amp = 1.20
            for idx in range(self._ripple_count):
                progress = (self._phase + idx / float(self._ripple_count)) % 1.0
                radius = min_radius + progress * (max_radius - min_radius)
                alpha = int((1.0 - progress) * 95.0 * amp)
                if alpha <= 0:
                    continue
                fill = QColor(base.red(), base.green(), base.blue(), int(alpha * 0.28))
                stroke = QColor(base.red(), base.green(), base.blue(), alpha)
                painter.setBrush(fill)
                painter.setPen(QPen(stroke, 2.0))
                painter.drawEllipse(center, radius, radius)

        icon_bg_radius = side * 0.18
        border_color = QColor(base.red(), base.green(), base.blue(), 125 if self._theme == "dark" else 95)
        painter.setPen(QPen(border_color, 1.5))
        painter.setBrush(self._icon_bg_color())
        painter.drawEllipse(center, icon_bg_radius, icon_bg_radius)

        if not self._icon_pixmap.isNull():
            icon_side = int(icon_bg_radius * 1.5)
            icon = self._icon_pixmap.scaled(icon_side, icon_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = int(center.x() - icon.width() / 2.0)
            y = int(center.y() - icon.height() / 2.0)
            painter.drawPixmap(x, y, icon)
        else:
            text_rect = QRectF(
                center.x() - icon_bg_radius,
                center.y() - icon_bg_radius,
                icon_bg_radius * 2.0,
                icon_bg_radius * 2.0,
            )
            painter.setPen(QColor(44, 70, 116))
            painter.drawText(text_rect, Qt.AlignCenter, "Voice")

        status_rect = QRectF(20.0, rect.height() - 78.0, rect.width() - 40.0, 56.0)
        painter.setPen(self._status_text_color())
        painter.drawText(status_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self._status_text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self._press_global_pos = event.globalPos()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            if self._press_global_pos is not None:
                moved = event.globalPos() - self._press_global_pos
                if moved.manhattanLength() > 6:
                    self._dragging = True
            if self._dragging:
                self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            was_dragging = self._dragging
            self._drag_offset = None
            self._press_global_pos = None
            self._dragging = False
            if was_dragging:
                event.accept()
                return
            if self._is_transcribing or self._is_agent_running:
                event.accept()
                return
            if self._is_listening:
                wav_bytes = self._stop_listening()
                self._start_stt(wav_bytes)
            else:
                self._start_listening()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        if self._is_listening:
            self._stop_listening()
        else:
            self._set_ripple_active(False)
        if self._stt_worker is not None:
            try:
                self._stt_worker.quit()
                self._stt_worker.wait(1000)
            except Exception:
                pass
            self._stt_worker = None
        if self._openclaw_worker is not None:
            try:
                self._openclaw_worker.requestInterruption()
                self._openclaw_worker.wait(1000)
                if self._openclaw_worker.isRunning():
                    self._openclaw_worker.terminate()
                    self._openclaw_worker.wait(500)
            except Exception:
                pass
            self._openclaw_worker = None
        self.closed.emit()
        super().closeEvent(event)
