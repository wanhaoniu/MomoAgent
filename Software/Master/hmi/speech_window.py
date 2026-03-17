"""Floating speech input window with configurable STT/TTS providers."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
try:
    import sounddevice as sd
except Exception:
    sd = None
from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget
from dotenv import dotenv_values, load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_DOTENV_PATHS = tuple(
    path for path in (REPO_ROOT / ".env", REPO_ROOT / "env") if path.exists()
)
for _dotenv_path in REPO_DOTENV_PATHS:
    load_dotenv(dotenv_path=_dotenv_path, override=False)

STT_PROVIDER_DEFAULT = "groq"
TTS_PROVIDER_DEFAULT = "groq"

GROQ_API_KEY_FALLBACK = os.getenv("GROQ_API_KEY")
GROQ_STT_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL_DEFAULT = "whisper-large-v3"

GROQ_TTS_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/speech"
GROQ_TTS_MODEL_DEFAULT = "canopylabs/orpheus-v1-english"
GROQ_TTS_VOICE_DEFAULT = "troy"
GROQ_TTS_RESPONSE_FORMAT_DEFAULT = "wav"
GROQ_TTS_TIMEOUT_SEC_DEFAULT = 45.0
GROQ_TTS_MAX_CHARS_DEFAULT = 180

OPENCLAW_BIN_DEFAULT = "openclaw"
OPENCLAW_AGENT_ID_DEFAULT = "main"
OPENCLAW_TIMEOUT_SEC_DEFAULT = 90.0
OPENCLAW_SKILL_NAME_DEFAULT = "soarmmoce-control"
SDK_SRC = REPO_ROOT / "sdk" / "src"

OPENCLAW_THINKING_DEFAULT = "minimal"


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


def _runtime_env_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for dotenv_path in REPO_DOTENV_PATHS:
        try:
            payload = dotenv_values(dotenv_path)
        except Exception:
            continue
        for key, value in payload.items():
            if key and value is not None:
                values[str(key)] = str(value)
    return values


def _runtime_env_get(name: str, default: Optional[str] = None, env_values: Optional[Dict[str, str]] = None) -> Optional[str]:
    if env_values is not None and name in env_values:
        return str(env_values[name])
    current = os.getenv(name)
    if current is not None:
        return str(current)
    return default


def _runtime_env_bool(name: str, default: bool, env_values: Optional[Dict[str, str]] = None) -> bool:
    raw = _runtime_env_get(name, None, env_values)
    if raw is None:
        return bool(default)
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _log_tts(message: str) -> None:
    print(f"[Speech:TTS] {message}", flush=True)


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
                # OpenClaw may stream intermediate thoughts as multiple payload items;
                # prefer the last non-empty item as final assistant reply.
                return texts[-1].strip()
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


def _looks_like_node_request(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    cn_hits = ("节点", "发布一个节点", "哪个节点", "node agent", "nodes")
    en_hits = ("which node", "need a node", "specify a node", "publish a node")
    return any(x in raw for x in cn_hits) or any(x in raw for x in en_hits)


def _looks_like_python_missing(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    hits = ("python command is not found", "command not found", "`python`", "python: not found")
    return any(x in raw for x in hits)


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
    return any(x in raw for x in hits)


def _build_robot_control_message(user_text: str, skill_name: str) -> str:
    text = str(user_text or "").strip()
    return text


def _sanitize_openclaw_reply(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "你在执行机械臂控制模式" not in raw:
        return raw

    for marker in ("机械臂已", "夹爪已", "执行成功", "执行失败", "SKILL_NOT_AVAILABLE", "失败", "成功"):
        idx = raw.rfind(marker)
        if idx >= 0:
            cleaned = raw[idx:].strip()
            if cleaned:
                return cleaned
    return raw


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


def _extract_http_error_message(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", "")).strip()
            if msg:
                return msg
        msg = str(payload.get("message", "")).strip()
        if msg:
            return msg

    text = str(response.text or "").strip()
    if text:
        return text
    return fallback


def _format_stt_exception(provider: str, url: str, exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ConnectionError):
        detail = str(exc).strip()
        message = f"STT 服务连接失败: {url}"
        if detail:
            message += f"\n详细错误: {detail}"
        return message
    return str(exc).strip() or f"{str(provider or '').strip().lower() or 'stt'} failed"


def _is_retryable_stt_request_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
        ),
    )


def _build_optional_bearer_headers(api_key: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _split_text_for_tts(text: str, max_chars: int) -> List[str]:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []

    limit = max(32, int(max_chars))
    if len(raw) <= limit:
        return [raw]

    chunks: List[str] = []
    pending: List[str] = []
    pending_len = 0

    def _flush_pending():
        nonlocal pending, pending_len
        if pending:
            chunks.append("".join(pending).strip())
        pending = []
        pending_len = 0

    for part in re.split(r"(?<=[。！？!?；;，,])", raw):
        piece = part.strip()
        if not piece:
            continue
        if len(piece) > limit:
            _flush_pending()
            for idx in range(0, len(piece), limit):
                sub = piece[idx : idx + limit].strip()
                if sub:
                    chunks.append(sub)
            continue
        if pending_len + len(piece) > limit:
            _flush_pending()
        pending.append(piece)
        pending_len += len(piece)

    _flush_pending()
    return [chunk for chunk in chunks if chunk]


def _decode_wav_bytes(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    if not wav_bytes:
        raise ValueError("TTS response is empty")

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        frame_count = int(wf.getnframes())
        frame_bytes = wf.readframes(frame_count)

    if sample_width == 1:
        audio = np.frombuffer(frame_bytes, dtype=np.uint8).astype(np.float32)
        audio = (audio - 128.0) / 128.0
    elif sample_width == 2:
        audio = np.frombuffer(frame_bytes, dtype="<i2")
    elif sample_width == 4:
        audio = np.frombuffer(frame_bytes, dtype="<i4")
    else:
        raise RuntimeError(f"Unsupported WAV sample width: {sample_width}")

    if channels > 1:
        audio = audio.reshape(-1, channels)
    return audio, sample_rate


_AUDIO_IO_LOCK = threading.RLock()
_AUDIO_PLAYBACK_POLL_MS = 20


def _to_float32_audio_frames(audio: np.ndarray) -> np.ndarray:
    arr = np.asarray(audio)
    if arr.ndim == 1:
        arr = arr[:, None]

    if arr.dtype == np.uint8:
        frames = (arr.astype(np.float32) - 128.0) / 128.0
    elif arr.dtype == np.int16:
        frames = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        frames = arr.astype(np.float32) / 2147483648.0
    else:
        frames = arr.astype(np.float32, copy=False)
    return np.ascontiguousarray(frames, dtype=np.float32)


def _find_playback_binary(name: str) -> Optional[str]:
    binary = shutil.which(name)
    if binary:
        return binary
    for candidate in (
        f"/usr/bin/{name}",
        f"/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _audio_player_command(backend: str, wav_path: str) -> Optional[List[str]]:
    backend_name = str(backend or "").strip().lower()
    if backend_name == "paplay":
        binary = _find_playback_binary("paplay")
        if binary:
            return [binary, wav_path]
    if backend_name == "aplay":
        binary = _find_playback_binary("aplay")
        if binary:
            return [binary, "-q", wav_path]
    if backend_name == "ffplay":
        binary = _find_playback_binary("ffplay")
        if binary:
            return [binary, "-nodisp", "-autoexit", "-loglevel", "error", wav_path]
    return None


def _iter_playback_backends(preferred: Optional[str] = None) -> List[str]:
    backend = str(
        preferred
        or _runtime_env_get("SOARMMOCE_TTS_PLAYBACK_BACKEND", "auto", _runtime_env_values())
        or "auto"
    ).strip().lower() or "auto"
    if backend != "auto":
        return [backend]

    backends: List[str] = []
    for candidate in ("paplay", "aplay", "ffplay"):
        if _find_playback_binary(candidate):
            backends.append(candidate)
    if backends:
        return backends
    if sd is not None:
        backends.append("sounddevice")
    return backends


def groq_text_to_speech(
    text: str,
    api_key: str,
    url: str,
    model: str,
    voice: str,
    response_format: str = GROQ_TTS_RESPONSE_FORMAT_DEFAULT,
    timeout_sec: float = GROQ_TTS_TIMEOUT_SEC_DEFAULT,
) -> bytes:
    content = str(text or "").strip()
    if not content:
        raise ValueError("Empty TTS text")

    response = requests.post(
        url,
        headers=_build_optional_bearer_headers(api_key),
        json={
            "model": str(model or "").strip() or GROQ_TTS_MODEL_DEFAULT,
            "voice": str(voice or "").strip() or GROQ_TTS_VOICE_DEFAULT,
            "input": content,
            "response_format": str(response_format or "").strip() or GROQ_TTS_RESPONSE_FORMAT_DEFAULT,
        },
        timeout=float(timeout_sec),
    )

    if not response.ok:
        raise RuntimeError(_extract_http_error_message(response, "TTS failed"))
    if not response.content:
        raise RuntimeError("TTS response is empty")
    return response.content


def _play_wav_bytes_with_command(wav_bytes: bytes, backend: str, stop_event: Optional[threading.Event] = None):
    stopper = stop_event or threading.Event()
    with tempfile.NamedTemporaryFile(prefix="soarmmoce_tts_", suffix=".wav", delete=False) as handle:
        wav_path = handle.name
        handle.write(wav_bytes)

    cmd = _audio_player_command(backend, wav_path)
    if not cmd:
        try:
            os.unlink(wav_path)
        except Exception:
            pass
        raise RuntimeError(f"Playback backend `{backend}` is not available")

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            if stopper.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
                return

            rc = proc.poll()
            if rc is not None:
                if rc != 0:
                    err = ""
                    try:
                        _, err = proc.communicate(timeout=0.2)
                    except Exception:
                        pass
                    raise RuntimeError(f"{backend} playback failed ({rc}): {str(err or '').strip()}")
                return
            time.sleep(_AUDIO_PLAYBACK_POLL_MS / 1000.0)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            os.unlink(wav_path)
        except Exception:
            pass


def _play_wav_bytes_with_sounddevice(wav_bytes: bytes, stop_event: Optional[threading.Event] = None):
    if sd is None:
        raise RuntimeError("sounddevice is not available")
    audio, sample_rate = _decode_wav_bytes(wav_bytes)
    frames = _to_float32_audio_frames(audio)
    if frames.size == 0:
        return

    stopper = stop_event or threading.Event()
    total_frames = int(frames.shape[0])
    channels = int(frames.shape[1])
    frame_cursor = 0

    def _callback(outdata, frame_count, _time_info, _status):
        nonlocal frame_cursor
        outdata.fill(0)
        if stopper.is_set():
            raise sd.CallbackStop

        next_cursor = min(frame_cursor + int(frame_count), total_frames)
        chunk = frames[frame_cursor:next_cursor]
        if chunk.size:
            outdata[: next_cursor - frame_cursor, :channels] = chunk
        frame_cursor = next_cursor
        if frame_cursor >= total_frames:
            raise sd.CallbackStop

    with _AUDIO_IO_LOCK:
        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            callback=_callback,
            blocksize=0,
        )
        with stream:
            while stream.active:
                if stopper.is_set():
                    try:
                        stream.abort()
                    except Exception:
                        pass
                    break
                sd.sleep(_AUDIO_PLAYBACK_POLL_MS)


def play_wav_bytes(
    wav_bytes: bytes,
    stop_event: Optional[threading.Event] = None,
    backend: Optional[str] = None,
):
    errors: List[str] = []
    with _AUDIO_IO_LOCK:
        for candidate in _iter_playback_backends(backend):
            try:
                _log_tts(f"playback backend try={candidate} bytes={len(wav_bytes)}")
                if candidate == "sounddevice":
                    _play_wav_bytes_with_sounddevice(wav_bytes, stop_event)
                else:
                    _play_wav_bytes_with_command(wav_bytes, candidate, stop_event)
                _log_tts(f"playback backend ok={candidate}")
                return
            except Exception as exc:
                _log_tts(f"playback backend failed={candidate} error={exc}")
                errors.append(f"{candidate}: {exc}")
        raise RuntimeError("All playback backends failed: " + "; ".join(errors))


def openai_compatible_audio_to_text(
    wav_bytes: bytes,
    api_key: str,
    url: str,
    model: str,
    language: str = "zh",
    timeout_sec: float = 45.0,
) -> str:
    if not wav_bytes:
        raise ValueError("Empty audio data")
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data: Dict[str, str] = {}
    if model:
        data["model"] = model
    if language:
        data["language"] = language

    data["response_format"] = "json"
    timeout = float(timeout_sec)
    last_exc: Optional[Exception] = None

    for attempt in range(3):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=data,
                files={"file": ("speech.wav", wav_bytes, "audio/wav")},
                timeout=timeout,
            )
            response.raise_for_status()

            try:
                payload: Any = response.json()
            except Exception:
                text_raw = response.text.strip()
                if text_raw:
                    return text_raw
                raise RuntimeError("STT response is empty")

            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    err_msg = str(err.get("message", "STT failed"))
                    raise RuntimeError(err_msg)

            text = _extract_text_from_stt_payload(payload)
            if not text:
                raise RuntimeError(f"STT response has no text: {payload}")
            return text
        except Exception as exc:
            last_exc = exc
            if attempt >= 2 or not _is_retryable_stt_request_error(exc):
                raise
            time.sleep(0.4 * (attempt + 1))

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("STT request failed unexpectedly")


class _SttWorker(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        wav_bytes: bytes,
        api_key: str,
        url: str,
        model: str,
        language: str,
        timeout_sec: float,
    ):
        super().__init__()
        self._wav_bytes = wav_bytes
        self._api_key = api_key
        self._url = url
        self._model = model
        self._language = str(language or "zh").strip() or "zh"
        self._timeout_sec = max(5.0, float(timeout_sec))

    def run(self):
        try:
            text = openai_compatible_audio_to_text(
                wav_bytes=self._wav_bytes,
                api_key=self._api_key,
                url=self._url,
                model=self._model,
                language=self._language,
                timeout_sec=self._timeout_sec,
            )
        except Exception as exc:
            self.failed.emit(_format_stt_exception("groq", self._url, exc))
            return
        self.done.emit(text)


class _GroqTtsWorker(QThread):
    failed = pyqtSignal(str)

    def __init__(
        self,
        text: str,
        api_key: str,
        url: str,
        model: str,
        voice: str,
        response_format: str,
        timeout_sec: float,
        max_chars: int,
        playback_backend: str = "auto",
    ):
        super().__init__()
        self._text = str(text or "").strip()
        self._api_key = str(api_key or "").strip()
        self._url = str(url or "").strip() or GROQ_TTS_URL_DEFAULT
        self._model = str(model or "").strip() or GROQ_TTS_MODEL_DEFAULT
        self._voice = str(voice or "").strip() or GROQ_TTS_VOICE_DEFAULT
        self._response_format = str(response_format or "").strip() or GROQ_TTS_RESPONSE_FORMAT_DEFAULT
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._max_chars = max(32, int(max_chars))
        self._playback_backend = str(playback_backend or "auto").strip() or "auto"
        self._stop_event = threading.Event()

    def stop(self):
        self.requestInterruption()
        self._stop_event.set()

    def run(self):
        try:
            chunks = _split_text_for_tts(self._text, self._max_chars)
            if not chunks:
                raise ValueError("Empty TTS text")
            for chunk in chunks:
                if self.isInterruptionRequested():
                    return
                wav_bytes = groq_text_to_speech(
                    text=chunk,
                    api_key=self._api_key,
                    url=self._url,
                    model=self._model,
                    voice=self._voice,
                    response_format=self._response_format,
                    timeout_sec=self._timeout_sec,
                )
                if self.isInterruptionRequested():
                    return
                play_wav_bytes(wav_bytes, self._stop_event, backend=self._playback_backend)
                if self.isInterruptionRequested():
                    return
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class _OpenClawAgentWorker(QThread):
    done = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        message: str,
        openclaw_bin: str,
        agent_id: str,
        session_id: str,
        local_mode: bool,
        timeout_sec: float,
        thinking: str,
        skill_name: str,
        robot_mode: bool,
        node_retry_count: int,
    ):
        super().__init__()
        self._message = str(message or "").strip()
        self._openclaw_bin = str(openclaw_bin or OPENCLAW_BIN_DEFAULT).strip() or OPENCLAW_BIN_DEFAULT
        self._agent_id = str(agent_id or OPENCLAW_AGENT_ID_DEFAULT).strip() or OPENCLAW_AGENT_ID_DEFAULT
        self._session_id = str(session_id or "").strip()
        self._local_mode = bool(local_mode)
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._thinking = str(thinking or OPENCLAW_THINKING_DEFAULT).strip() or OPENCLAW_THINKING_DEFAULT
        self._skill_name = str(skill_name or OPENCLAW_SKILL_NAME_DEFAULT).strip() or OPENCLAW_SKILL_NAME_DEFAULT
        self._robot_mode = bool(robot_mode)
        self._node_retry_count = max(0, int(node_retry_count))

    def _build_subprocess_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        if SDK_SRC.exists():
            existing_pp = str(env.get("PYTHONPATH", "")).strip()
            sdk_src_str = str(SDK_SRC.resolve())
            if existing_pp:
                env["PYTHONPATH"] = f"{sdk_src_str}:{existing_pp}"
            else:
                env["PYTHONPATH"] = sdk_src_str
        return env

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
        return cmd

    def _invoke_openclaw_once(self, message: str, session_id: str) -> Dict[str, Any]:
        cmd = self._build_agent_cmd(message=message, session_id=session_id)
        cwd = None
        env = self._build_subprocess_env()
        start_time = time.time()
        start_perf = time.perf_counter()
        proc: Optional[subprocess.CompletedProcess[str]] = None
        if self._robot_mode:
            skill_dir = Path.home() / ".openclaw" / "skills" / self._skill_name
            if skill_dir.exists() and skill_dir.is_dir():
                cwd = str(skill_dir)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
                check=False,
                cwd=cwd,
                env=env,
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
            return {
                "payload": payload,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
        except FileNotFoundError:
            raise RuntimeError(f"找不到 OpenClaw 可执行文件: {self._openclaw_bin}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("OpenClaw 调用超时")
        except Exception as exc:
            raise RuntimeError(f"OpenClaw 调用失败: {exc}") from exc
        finally:
            end_time = time.time()
            elapsed_sec = time.perf_counter() - start_perf
            self._log_openclaw_invoke_timing(
                start_time=start_time,
                end_time=end_time,
                elapsed_sec=elapsed_sec,
                returncode=proc.returncode if proc is not None else None,
                session_id=session_id,
                message=message,
            )

    def _log_openclaw_invoke_timing(
        self,
        *,
        start_time: float,
        end_time: float,
        elapsed_sec: float,
        returncode: Optional[int],
        session_id: str,
        message: str,
    ) -> None:
        print(
            "[Speech][OpenClawTiming] "
            f"start_time={start_time:.3f} "
            f"end_time={end_time:.3f} "
            f"elapsed_sec={elapsed_sec:.3f} "
            f"returncode={returncode if returncode is not None else 'n/a'} "
            f"session_id={'set' if session_id else 'new'} "
            f"message_len={len(str(message or ''))}",
            flush=True,
        )

    def _prepare_message(self, message: str, retry: bool = False) -> str:
        base = str(message or "").strip()
        if not self._robot_mode:
            return base
        return _build_robot_control_message(base, self._skill_name)

    def run(self):
        if not self._message:
            self.failed.emit("OpenClaw 输入为空")
            return

        if self.isInterruptionRequested():
            self.failed.emit("OpenClaw 请求已取消")
            return

        attempts = 1 + self._node_retry_count
        current_session = self._session_id
        for idx in range(attempts):
            retry = idx > 0
            try:
                result = self._invoke_openclaw_once(
                    message=self._prepare_message(self._message, retry=retry),
                    session_id=current_session,
                )
            except Exception as exc:
                self.failed.emit(str(exc))
                return

            payload = result.get("payload")
            stdout_text = str(result.get("stdout", "")).strip()
            current_session = _extract_openclaw_session_id(payload) or current_session

            reply = _extract_text_from_openclaw_payload(payload)
            if not reply:
                reply = stdout_text
            reply = str(reply or "").strip()
            if not reply:
                self.failed.emit("OpenClaw 未返回可用文本")
                return

            if _looks_like_node_request(reply) and idx + 1 < attempts:
                continue
            if _looks_like_dispatch_usage_error(reply) and idx + 1 < attempts:
                continue

            if _looks_like_node_request(reply):
                self.failed.emit(
                    "OpenClaw 仍在请求节点，未进入 soarmmoce-control 技能执行链路。"
                    "请检查 ~/.openclaw/skills 中技能安装状态。"
                )
                return
            if _looks_like_dispatch_usage_error(reply):
                self.failed.emit(
                    "OpenClaw 已进入技能链路，但工具脚本调用格式不正确。"
                    "应使用：python3 ~/.openclaw/skills/soarmmoce-control/scripts/soarmmoce_tools.py call --name ... --args ..."
                )
                return

            if _looks_like_python_missing(reply):
                self.failed.emit(
                    "OpenClaw 已进入技能链路，但执行环境缺少 `python` 命令。"
                    "请在系统中提供 `python` 或让技能脚本固定使用 python3。"
                )
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
    agent_session_changed = pyqtSignal(str)
    tts_failed = pyqtSignal(str)

    def __init__(self, title: str, icon_path: Optional[Path] = None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(300, 300)
        runtime_env = _runtime_env_values()

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
        self._is_tts_running = False
        self._status_text = "点击开始说话"
        self._last_text = ""
        self._last_agent_reply = ""

        self._sample_rate = 16000
        self._channels = 1
        self._audio_stream: Optional[sd.InputStream] = None
        self._audio_chunks: List[np.ndarray] = []
        self._stt_worker: Optional[_SttWorker] = None
        self._openclaw_worker: Optional[_OpenClawAgentWorker] = None
        self._tts_worker: Optional[QThread] = None

        self._stt_provider = STT_PROVIDER_DEFAULT
        self._stt_api_key = str(
            os.getenv("SOARMMOCE_STT_API_KEY")
            or os.getenv("GROQ_API_KEY")
            or GROQ_API_KEY_FALLBACK
            or ""
        ).strip()
        self._stt_url = os.getenv("GROQ_STT_URL", GROQ_STT_URL_DEFAULT).strip() or GROQ_STT_URL_DEFAULT
        self._stt_model = os.getenv("GROQ_STT_MODEL", GROQ_STT_MODEL_DEFAULT).strip() or GROQ_STT_MODEL_DEFAULT
        try:
            self._stt_timeout_sec = float(
                str(os.getenv("SOARMMOCE_STT_TIMEOUT_SEC", "45.0")).strip()
            )
        except Exception:
            self._stt_timeout_sec = 45.0
        self._stt_timeout_sec = max(5.0, self._stt_timeout_sec)

        self._tts_provider = TTS_PROVIDER_DEFAULT
        self._tts_enabled = _runtime_env_bool("SOARMMOCE_TTS_ENABLED", True, runtime_env)
        self._groq_api_key = str(
            _runtime_env_get("SOARMMOCE_TTS_API_KEY", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_API_KEY", None, runtime_env)
            or _runtime_env_get("GROQ_API_KEY", None, runtime_env)
            or GROQ_API_KEY_FALLBACK
            or ""
        ).strip()
        self._groq_tts_url = (
            _runtime_env_get("SOARMMOCE_TTS_URL", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_URL", None, runtime_env)
            or GROQ_TTS_URL_DEFAULT
        ).strip() or GROQ_TTS_URL_DEFAULT
        self._groq_tts_model = (
            _runtime_env_get("SOARMMOCE_TTS_MODEL", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_MODEL", None, runtime_env)
            or GROQ_TTS_MODEL_DEFAULT
        ).strip() or GROQ_TTS_MODEL_DEFAULT
        self._groq_tts_voice = (
            _runtime_env_get("SOARMMOCE_TTS_VOICE", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_VOICE", None, runtime_env)
            or GROQ_TTS_VOICE_DEFAULT
        ).strip() or GROQ_TTS_VOICE_DEFAULT
        self._groq_tts_response_format = (
            (
                _runtime_env_get("SOARMMOCE_TTS_RESPONSE_FORMAT", None, runtime_env)
                or _runtime_env_get("GROQ_TTS_RESPONSE_FORMAT", None, runtime_env)
                or GROQ_TTS_RESPONSE_FORMAT_DEFAULT
            ).strip()
            or GROQ_TTS_RESPONSE_FORMAT_DEFAULT
        )
        try:
            self._groq_tts_timeout_sec = float(
                str(
                    _runtime_env_get("SOARMMOCE_TTS_TIMEOUT_SEC", None, runtime_env)
                    or _runtime_env_get("GROQ_TTS_TIMEOUT_SEC", None, runtime_env)
                    or str(GROQ_TTS_TIMEOUT_SEC_DEFAULT)
                ).strip()
            )
        except Exception:
            self._groq_tts_timeout_sec = GROQ_TTS_TIMEOUT_SEC_DEFAULT
        self._groq_tts_timeout_sec = max(5.0, self._groq_tts_timeout_sec)
        try:
            self._groq_tts_max_chars = max(
                32,
                int(
                    str(
                        _runtime_env_get("SOARMMOCE_TTS_MAX_CHARS", None, runtime_env)
                        or _runtime_env_get("GROQ_TTS_MAX_CHARS", None, runtime_env)
                        or str(GROQ_TTS_MAX_CHARS_DEFAULT)
                    ).strip()
                ),
            )
        except Exception:
            self._groq_tts_max_chars = GROQ_TTS_MAX_CHARS_DEFAULT
        self._tts_playback_backend = (
            _runtime_env_get("SOARMMOCE_TTS_PLAYBACK_BACKEND", "auto", runtime_env) or "auto"
        ).strip() or "auto"

        self._openclaw_enabled = _env_bool("OPENCLAW_ENABLED", True)
        self._openclaw_bin = str(os.getenv("OPENCLAW_BIN", OPENCLAW_BIN_DEFAULT)).strip() or OPENCLAW_BIN_DEFAULT
        self._openclaw_agent_id = str(
            os.getenv("OPENCLAW_AGENT_ID", OPENCLAW_AGENT_ID_DEFAULT)
        ).strip() or OPENCLAW_AGENT_ID_DEFAULT
        self._openclaw_skill_name = str(
            os.getenv("OPENCLAW_SKILL_NAME", OPENCLAW_SKILL_NAME_DEFAULT)
        ).strip() or OPENCLAW_SKILL_NAME_DEFAULT
        self._openclaw_local_mode = _env_bool("OPENCLAW_LOCAL", False)
        self._openclaw_robot_mode = _env_bool("OPENCLAW_ROBOT_MODE", True)
        self._openclaw_force_new_session = _env_bool("OPENCLAW_FORCE_NEW_SESSION", False)
        try:
            self._openclaw_node_retry_count = max(
                0, int(str(os.getenv("OPENCLAW_NODE_RETRY_COUNT", "2")).strip())
            )
        except Exception:
            self._openclaw_node_retry_count = 2
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

    def set_stt_config(
        self,
        *,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_sec: Optional[float] = None,
    ):
        self._stt_provider = STT_PROVIDER_DEFAULT
        if api_key is not None:
            self._stt_api_key = str(api_key or "").strip()
        if url is not None:
            self._stt_url = str(url or "").strip() or GROQ_STT_URL_DEFAULT
        if model is not None:
            self._stt_model = str(model or "").strip() or GROQ_STT_MODEL_DEFAULT
        if timeout_sec is not None:
            try:
                self._stt_timeout_sec = max(5.0, float(timeout_sec))
            except Exception:
                pass

    def set_groq_config(self, api_key: str, stt_url: Optional[str] = None, model: Optional[str] = None):
        self.set_stt_config(provider="groq", api_key=api_key, url=stt_url, model=model)

    def set_groq_tts_config(
        self,
        enabled: Optional[bool] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        response_format: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        max_chars: Optional[int] = None,
    ):
        self._tts_provider = "groq"
        if enabled is not None:
            self._tts_enabled = bool(enabled)
        if api_key is not None:
            self._groq_api_key = str(api_key or "").strip()
        if url is not None:
            self._groq_tts_url = str(url or "").strip() or GROQ_TTS_URL_DEFAULT
        if model is not None:
            self._groq_tts_model = str(model or "").strip() or GROQ_TTS_MODEL_DEFAULT
        if voice is not None:
            self._groq_tts_voice = str(voice or "").strip() or GROQ_TTS_VOICE_DEFAULT
        if response_format is not None:
            self._groq_tts_response_format = (
                str(response_format or "").strip() or GROQ_TTS_RESPONSE_FORMAT_DEFAULT
            )
        if timeout_sec is not None:
            try:
                self._groq_tts_timeout_sec = max(5.0, float(timeout_sec))
            except Exception:
                pass
        if max_chars is not None:
            try:
                self._groq_tts_max_chars = max(32, int(max_chars))
            except Exception:
                pass

    def set_openclaw_config(
        self,
        enabled: bool = True,
        openclaw_bin: Optional[str] = None,
        agent_id: Optional[str] = None,
        skill_name: Optional[str] = None,
        local_mode: Optional[bool] = None,
        robot_mode: Optional[bool] = None,
        force_new_session: Optional[bool] = None,
        node_retry_count: Optional[int] = None,
        thinking: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        session_id: Optional[str] = None,
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
        if skill_name is not None:
            val = str(skill_name).strip()
            if val:
                self._openclaw_skill_name = val
        if local_mode is not None:
            self._openclaw_local_mode = bool(local_mode)
        if robot_mode is not None:
            self._openclaw_robot_mode = bool(robot_mode)
        if force_new_session is not None:
            self._openclaw_force_new_session = bool(force_new_session)
        if node_retry_count is not None:
            try:
                self._openclaw_node_retry_count = max(0, int(node_retry_count))
            except Exception:
                pass
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

    # Backward compatibility for old call sites.
    def set_minimax_config(self, api_key: str, stt_url: Optional[str] = None, model: Optional[str] = None):
        self.set_groq_config(api_key=api_key, stt_url=stt_url, model=model)

    def _stop_tts_playback(self, wait_ms: int = 1000):
        worker = self._tts_worker
        if worker is None:
            self._is_tts_running = False
            return

        try:
            worker.stop()
        except Exception:
            try:
                worker.requestInterruption()
            except Exception:
                pass

        if wait_ms > 0:
            try:
                worker.wait(wait_ms)
            except Exception:
                pass
        if not worker.isRunning():
            self._tts_worker = None
            self._is_tts_running = False
        else:
            self._is_tts_running = True

    def _start_tts(self, reply_text: str):
        text = str(reply_text or "").strip()
        if not text or not self._tts_enabled:
            return

        self._stop_tts_playback(wait_ms=500)
        if self._tts_worker is not None and self._tts_worker.isRunning():
            msg = "语音播报仍在停止中，已跳过本次播报"
            self.tts_failed.emit(msg)
            return

        self._is_tts_running = True
        self._tts_provider = TTS_PROVIDER_DEFAULT
        _log_tts(
            "start "
            f"url={self._groq_tts_url} "
            f"model={self._groq_tts_model} "
            f"voice={self._groq_tts_voice} "
            f"backend={self._tts_playback_backend}"
        )
        self._tts_worker = _GroqTtsWorker(
            text=text,
            api_key=self._groq_api_key,
            url=self._groq_tts_url,
            model=self._groq_tts_model,
            voice=self._groq_tts_voice,
            response_format=self._groq_tts_response_format,
            timeout_sec=self._groq_tts_timeout_sec,
            max_chars=self._groq_tts_max_chars,
            playback_backend=self._tts_playback_backend,
        )
        self._tts_worker.failed.connect(self._on_tts_failed)
        self._tts_worker.finished.connect(self._on_tts_finished)
        self._tts_worker.start()

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
        self._stop_tts_playback(wait_ms=1500)
        if self._tts_worker is not None and self._tts_worker.isRunning():
            self._status_text = "语音播报停止中，请稍候再说"
            self.update()
            return
        try:
            self._audio_chunks = []
            with _AUDIO_IO_LOCK:
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
            with _AUDIO_IO_LOCK:
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

        self._stt_worker = _SttWorker(
            wav_bytes=wav_bytes,
            api_key=self._stt_api_key,
            url=self._stt_url,
            model=self._stt_model,
            language="zh",
            timeout_sec=self._stt_timeout_sec,
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

        session_id = self._openclaw_session_id
        if self._openclaw_force_new_session:
            session_id = uuid.uuid4().hex

        self._openclaw_worker = _OpenClawAgentWorker(
            message=text,
            openclaw_bin=self._openclaw_bin,
            agent_id=self._openclaw_agent_id,
            session_id=session_id,
            local_mode=self._openclaw_local_mode,
            timeout_sec=self._openclaw_timeout_sec,
            thinking=self._openclaw_thinking,
            skill_name=self._openclaw_skill_name,
            robot_mode=self._openclaw_robot_mode,
            node_retry_count=self._openclaw_node_retry_count,
        )
        self._openclaw_worker.done.connect(self._on_openclaw_done)
        self._openclaw_worker.failed.connect(self._on_openclaw_failed)
        self._openclaw_worker.finished.connect(self._on_openclaw_finished)
        self._openclaw_worker.start()

    def _on_openclaw_done(self, reply: str, session_id: str):
        reply_text = _sanitize_openclaw_reply(str(reply or "").strip())
        self._last_agent_reply = reply_text
        if reply_text:
            self._status_text = f"Momo: {reply_text}"
            self.agent_reply_ready.emit(reply_text)
            self._start_tts(reply_text)
        else:
            self._status_text = "OpenClaw 未返回文本"
            self.agent_failed.emit(self._status_text)

        sid = str(session_id or "").strip()
        if (not self._openclaw_force_new_session) and sid and sid != self._openclaw_session_id:
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

    def _on_tts_failed(self, error_text: str):
        self._is_tts_running = False
        msg = str(error_text or "").strip() or "语音播报失败"
        _log_tts(f"failed {msg}")
        if self._last_agent_reply:
            self._status_text = f"Momo: {self._last_agent_reply}\n语音播报失败"
        else:
            self._status_text = msg
        self.tts_failed.emit(msg)
        self.update()

    def _on_tts_finished(self):
        self._is_tts_running = False
        _log_tts("finished")
        self._tts_worker = None
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
        self._stop_tts_playback(wait_ms=1000)
        self.closed.emit()
        super().closeEvent(event)
