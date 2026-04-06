from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

try:
    import sounddevice as sd

    _SOUNDDEVICE_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:
    sd = None
    _SOUNDDEVICE_IMPORT_ERROR = exc

from .config import (
    COSYVOICE_TTS_ENDOFPROMPT_TOKEN,
    COSYVOICE_TTS_INSTRUCT_PREFIX_DEFAULT,
    COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT,
    COSYVOICE_TTS_MODE_DEFAULT,
    COSYVOICE_TTS_PROMPT_TEXT_DEFAULT,
    COSYVOICE_TTS_SAMPLE_RATE_DEFAULT,
    COSYVOICE_TTS_TEXT_PREFIX_DEFAULT,
    GROQ_TTS_MODEL_DEFAULT,
    GROQ_TTS_RESPONSE_FORMAT_DEFAULT,
    GROQ_TTS_VOICE_DEFAULT,
    MIMO_TTS_MODEL_DEFAULT,
    MIMO_TTS_RESPONSE_FORMAT_DEFAULT,
    MIMO_TTS_VOICE_DEFAULT,
    AudioConfig,
    SttConfig,
    TtsConfig,
)

_AUDIO_IO_LOCK = threading.RLock()
_AUDIO_PLAYBACK_POLL_MS = 20


def audio_input_unavailable_message() -> str:
    if sd is not None:
        return "录音不可用"
    detail = str(_SOUNDDEVICE_IMPORT_ERROR).strip() if _SOUNDDEVICE_IMPORT_ERROR else ""
    if detail:
        return f"录音不可用: sounddevice/PortAudio 未就绪 ({detail})"
    return "录音不可用: sounddevice/PortAudio 未就绪"


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
    return text or fallback


def _extract_text_from_stt_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        texts = [_extract_text_from_stt_payload(item) for item in payload]
        return " ".join(text for text in texts if text).strip()
    if isinstance(payload, dict):
        for key in ("text", "transcript", "asr_text", "recognized_text", "result_text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("result", "data", "output", "choices", "results"):
            if key in payload:
                nested = _extract_text_from_stt_payload(payload[key])
                if nested:
                    return nested
    return ""


def _is_retryable_stt_request_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError),
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

    def _flush_pending() -> None:
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
            for index in range(0, len(piece), limit):
                sub = piece[index : index + limit].strip()
                if sub:
                    chunks.append(sub)
            continue
        if pending_len + len(piece) > limit:
            _flush_pending()
        pending.append(piece)
        pending_len += len(piece)
    _flush_pending()
    return [chunk for chunk in chunks if chunk]


def _looks_like_wav_bytes(payload: bytes) -> bool:
    raw = bytes(payload or b"")
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _wrap_pcm16le_to_wav_bytes(
    pcm_bytes: bytes,
    sample_rate: int,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    raw = bytes(pcm_bytes or b"")
    if not raw:
        raise ValueError("TTS response is empty")
    with io.BytesIO() as buf:
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(max(1, int(channels)))
            wf.setsampwidth(max(1, int(sample_width)))
            wf.setframerate(max(8000, int(sample_rate)))
            wf.writeframes(raw)
        return buf.getvalue()


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


def _extract_mimo_audio_bytes(payload: Any, response_format: str) -> bytes:
    if not isinstance(payload, dict):
        raise RuntimeError("MIMO TTS returned an unexpected payload")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("MIMO TTS response is missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("MIMO TTS response is missing message")
    audio = message.get("audio")
    if not isinstance(audio, dict):
        raise RuntimeError("MIMO TTS response is missing audio data")
    encoded = str(audio.get("data", "")).strip()
    if not encoded:
        raise RuntimeError("MIMO TTS audio data is empty")
    try:
        audio_bytes = base64.b64decode(encoded)
    except Exception as exc:
        raise RuntimeError(f"MIMO TTS audio decode failed: {exc}") from exc
    if _looks_like_wav_bytes(audio_bytes):
        return audio_bytes
    format_name = str(response_format or "").strip().lower() or MIMO_TTS_RESPONSE_FORMAT_DEFAULT
    if format_name in {"pcm", "pcm16", "pcm16le", "raw"}:
        return _wrap_pcm16le_to_wav_bytes(audio_bytes, sample_rate=24000)
    raise RuntimeError(f"MIMO TTS returned unsupported audio format: {format_name}")


def _normalize_cosyvoice_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().lower() or COSYVOICE_TTS_MODE_DEFAULT
    if mode in {"cross_lingual", "zero_shot", "instruct2"}:
        return mode
    return COSYVOICE_TTS_MODE_DEFAULT


def _ensure_cosyvoice3_text_prefix(value: Optional[str], fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if COSYVOICE_TTS_ENDOFPROMPT_TOKEN in text:
        return text
    return f"{COSYVOICE_TTS_TEXT_PREFIX_DEFAULT}{text}"


def _ensure_cosyvoice3_instruct_text(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT
    if COSYVOICE_TTS_ENDOFPROMPT_TOKEN in text:
        return text
    if text.lower().startswith("you are a helpful assistant"):
        return f"{text}{COSYVOICE_TTS_ENDOFPROMPT_TOKEN}"
    return f"{COSYVOICE_TTS_INSTRUCT_PREFIX_DEFAULT}{text}{COSYVOICE_TTS_ENDOFPROMPT_TOKEN}"


def _default_input_device_index() -> Optional[int]:
    if sd is None:
        return None
    try:
        device = sd.default.device
    except Exception:
        return None
    if isinstance(device, (list, tuple)):
        device = device[0] if device else None
    try:
        parsed = int(device)
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _available_input_device_indices(min_channels: int = 1) -> List[int]:
    indices: List[int] = []
    default_index = _default_input_device_index()
    if default_index is not None:
        indices.append(default_index)
    if sd is None:
        return indices
    try:
        devices = sd.query_devices()
    except Exception:
        return indices
    for index, info in enumerate(devices):
        try:
            max_input_channels = int(info.get("max_input_channels", 0))
        except Exception:
            max_input_channels = 0
        if max_input_channels < max(1, int(min_channels)):
            continue
        if index not in indices:
            indices.append(index)
    return indices


def _resolve_input_stream_settings(
    target_rate: int,
    channels: int,
    dtype: str = "int16",
) -> Tuple[int, int, str]:
    if sd is None:
        raise RuntimeError(audio_input_unavailable_message())
    last_error: Optional[Exception] = None
    for device_index in _available_input_device_indices(min_channels=channels):
        try:
            device_info = sd.query_devices(device_index, "input")
        except Exception as exc:
            last_error = exc
            continue
        device_name = str(device_info.get("name", f"#{device_index}")).strip() or f"#{device_index}"
        candidate_rates: List[int] = []
        for candidate in (
            target_rate,
            device_info.get("default_samplerate"),
            48000,
            44100,
            32000,
            24000,
            22050,
            16000,
            8000,
        ):
            try:
                rate = int(round(float(candidate)))
            except Exception:
                continue
            if rate >= 8000 and rate not in candidate_rates:
                candidate_rates.append(rate)
        for rate in candidate_rates:
            try:
                sd.check_input_settings(
                    device=device_index,
                    samplerate=rate,
                    channels=max(1, int(channels)),
                    dtype=dtype,
                )
                return device_index, rate, device_name
            except Exception as exc:
                last_error = exc
    prefix = "未找到可用录音设备" if _default_input_device_index() is None else "没有找到可用的录音设备采样率"
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(prefix + detail)


def _resample_int16_audio(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    arr = np.asarray(audio, dtype=np.int16)
    if arr.size == 0 or src_rate == dst_rate:
        return arr
    squeeze = False
    if arr.ndim == 1:
        arr = arr[:, None]
        squeeze = True
    src_frames = int(arr.shape[0])
    if src_frames <= 1:
        return arr[:, 0] if squeeze else arr
    dst_frames = max(1, int(round(src_frames * float(dst_rate) / float(src_rate))))
    src_positions = np.arange(src_frames, dtype=np.float64)
    dst_positions = np.linspace(0.0, float(src_frames - 1), num=dst_frames, dtype=np.float64)
    out = np.empty((dst_frames, arr.shape[1]), dtype=np.float32)
    for channel_idx in range(arr.shape[1]):
        out[:, channel_idx] = np.interp(
            dst_positions,
            src_positions,
            arr[:, channel_idx].astype(np.float32),
        )
    clipped = np.clip(np.rint(out), -32768, 32767).astype(np.int16)
    return clipped[:, 0] if squeeze else clipped


def _find_playback_binary(name: str) -> Optional[str]:
    binary = shutil.which(name)
    if binary:
        return binary
    for candidate in (f"/usr/bin/{name}", f"/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _audio_player_command(backend: str, wav_path: str) -> Optional[List[str]]:
    backend_name = str(backend or "").strip().lower()
    if backend_name == "afplay":
        binary = _find_playback_binary("afplay")
        if binary:
            return [binary, wav_path]
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
    backend = str(preferred or "auto").strip().lower() or "auto"
    if backend != "auto":
        return [backend]
    backends: List[str] = []
    for candidate in ("afplay", "paplay", "aplay", "ffplay"):
        if _find_playback_binary(candidate):
            backends.append(candidate)
    if sd is not None:
        backends.append("sounddevice")
    return backends


def _play_wav_bytes_with_command(
    wav_bytes: bytes,
    backend: str,
    stop_event: Optional[threading.Event] = None,
) -> None:
    stopper = stop_event or threading.Event()
    with tempfile.NamedTemporaryFile(prefix="momo_agent_tts_", suffix=".wav", delete=False) as handle:
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


def _play_wav_bytes_with_sounddevice(
    wav_bytes: bytes,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if sd is None:
        raise RuntimeError(audio_input_unavailable_message())
    audio, sample_rate = _decode_wav_bytes(wav_bytes)
    frames = _to_float32_audio_frames(audio)
    if frames.size == 0:
        return
    stopper = stop_event or threading.Event()
    total_frames = int(frames.shape[0])
    channels = int(frames.shape[1])
    frame_cursor = 0

    def _callback(outdata, frame_count, _time_info, _status) -> None:
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
) -> None:
    errors: List[str] = []
    with _AUDIO_IO_LOCK:
        for candidate in _iter_playback_backends(backend):
            try:
                if candidate == "sounddevice":
                    _play_wav_bytes_with_sounddevice(wav_bytes, stop_event)
                else:
                    _play_wav_bytes_with_command(wav_bytes, candidate, stop_event)
                return
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
    raise RuntimeError("All playback backends failed: " + "; ".join(errors))


def openai_compatible_audio_to_text(wav_bytes: bytes, config: SttConfig) -> str:
    if not wav_bytes:
        raise ValueError("Empty audio data")
    headers: Dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    data: Dict[str, str] = {}
    if config.model:
        data["model"] = config.model
    if config.language:
        data["language"] = config.language
    data["response_format"] = "json"
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = requests.post(
                config.url,
                headers=headers,
                data=data,
                files={"file": ("speech.wav", wav_bytes, "audio/wav")},
                timeout=float(config.timeout_sec),
            )
            if not response.ok:
                raise RuntimeError(_extract_http_error_message(response, "STT failed"))
            try:
                payload = response.json()
            except Exception:
                text_raw = response.text.strip()
                if text_raw:
                    return text_raw
                raise RuntimeError("STT response is empty")
            text = _extract_text_from_stt_payload(payload)
            if not text:
                raise RuntimeError(f"STT response has no text: {payload}")
            return text
        except Exception as exc:
            last_exc = exc
            if attempt >= 2 or not _is_retryable_stt_request_error(exc):
                raise
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(str(last_exc or "STT request failed unexpectedly"))


def transcribe_audio(wav_bytes: bytes, config: SttConfig) -> str:
    return openai_compatible_audio_to_text(wav_bytes, config)


def openai_compatible_text_to_speech(
    text: str,
    api_key: str,
    url: str,
    model: str,
    voice: str,
    response_format: str,
    timeout_sec: float,
    fallback_model: str,
    fallback_voice: str,
    fallback_response_format: str,
    error_label: str,
) -> bytes:
    content = str(text or "").strip()
    if not content:
        raise ValueError("Empty TTS text")
    payload = {
        "model": str(model or "").strip() or fallback_model,
        "input": content,
        "response_format": str(response_format or "").strip() or fallback_response_format,
    }
    voice_value = str(voice or "").strip() or fallback_voice
    if voice_value:
        payload["voice"] = voice_value
    response = requests.post(
        url,
        headers=_build_optional_bearer_headers(api_key),
        json=payload,
        timeout=float(timeout_sec),
    )
    if not response.ok:
        raise RuntimeError(_extract_http_error_message(response, error_label))
    if not response.content:
        raise RuntimeError("TTS response is empty")
    return response.content


def groq_text_to_speech(text: str, config: TtsConfig) -> bytes:
    return openai_compatible_text_to_speech(
        text=text,
        api_key=config.groq_api_key,
        url=config.groq_url,
        model=config.groq_model,
        voice=config.groq_voice,
        response_format=config.groq_response_format,
        timeout_sec=config.groq_timeout_sec,
        fallback_model=GROQ_TTS_MODEL_DEFAULT,
        fallback_voice=GROQ_TTS_VOICE_DEFAULT,
        fallback_response_format=GROQ_TTS_RESPONSE_FORMAT_DEFAULT,
        error_label="Groq TTS failed",
    )


def mimo_text_to_speech(text: str, config: TtsConfig) -> bytes:
    content = str(text or "").strip()
    if not content:
        raise ValueError("Empty TTS text")
    payload = {
        "model": str(config.model or "").strip() or MIMO_TTS_MODEL_DEFAULT,
        "messages": [{"role": "assistant", "content": content}],
        "audio": {
            "format": str(config.response_format or "").strip() or MIMO_TTS_RESPONSE_FORMAT_DEFAULT,
            "voice": str(config.voice or "").strip() or MIMO_TTS_VOICE_DEFAULT,
        },
    }
    response = requests.post(
        config.url,
        headers=_build_optional_bearer_headers(config.api_key),
        json=payload,
        timeout=float(config.timeout_sec),
    )
    if not response.ok:
        raise RuntimeError(_extract_http_error_message(response, "MIMO TTS failed"))
    try:
        response_payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"MIMO TTS returned non-JSON response: {exc}") from exc
    return _extract_mimo_audio_bytes(response_payload, str(payload["audio"].get("format", "")))


def cosyvoice_http_text_to_speech(text: str, config: TtsConfig) -> bytes:
    content = str(text or "").strip()
    if not content:
        raise ValueError("Empty TTS text")
    service_url = str(config.cosyvoice_url or "").strip()
    if not service_url:
        raise ValueError("Empty CosyVoice service URL")
    req_mode = _normalize_cosyvoice_mode(config.cosyvoice_mode)
    if re.search(r"/inference_(cross_lingual|zero_shot|instruct2)/?$", service_url):
        url = service_url.rstrip("/")
    else:
        url = f"{service_url.rstrip('/')}/inference_{req_mode}"
    prompt_path = Path(str(config.cosyvoice_prompt_wav or "").strip()).expanduser()
    if req_mode in {"cross_lingual", "zero_shot", "instruct2"} and not prompt_path.is_file():
        raise FileNotFoundError(f"CosyVoice prompt wav not found: {prompt_path}")
    data: Dict[str, str] = {"tts_text": content}
    if req_mode == "zero_shot":
        prompt_text_value = str(config.cosyvoice_prompt_text or "").strip()
        if not prompt_text_value:
            raise ValueError("CosyVoice zero_shot mode requires prompt_text")
        data["prompt_text"] = _ensure_cosyvoice3_text_prefix(
            prompt_text_value,
            COSYVOICE_TTS_PROMPT_TEXT_DEFAULT,
        )
    elif req_mode == "cross_lingual":
        data["tts_text"] = _ensure_cosyvoice3_text_prefix(content, content)
    elif req_mode == "instruct2":
        data["instruct_text"] = _ensure_cosyvoice3_instruct_text(config.cosyvoice_instruct_text)
    with prompt_path.open("rb") as handle:
        response = requests.post(
            url,
            data=data,
            files={"prompt_wav": (prompt_path.name, handle, "audio/wav")},
            timeout=float(config.cosyvoice_timeout_sec),
            stream=True,
        )
        if not response.ok:
            raise RuntimeError(_extract_http_error_message(response, "CosyVoice TTS failed"))
        audio_bytes = b"".join(chunk for chunk in response.iter_content(chunk_size=16384) if chunk)
    if not audio_bytes:
        raise RuntimeError("CosyVoice TTS response is empty")
    if _looks_like_wav_bytes(audio_bytes):
        return audio_bytes
    return _wrap_pcm16le_to_wav_bytes(audio_bytes, sample_rate=config.cosyvoice_sample_rate)


def synthesize_text(text: str, config: TtsConfig) -> bytes:
    provider = str(config.provider or "").strip().lower()
    if provider == "cosyvoice":
        return cosyvoice_http_text_to_speech(text, config)
    if provider == "groq":
        return groq_text_to_speech(text, config)
    return mimo_text_to_speech(text, config)


def _tts_chunk_limit(config: TtsConfig) -> int:
    provider = str(config.provider or "").strip().lower()
    if provider == "cosyvoice":
        return max(32, int(config.cosyvoice_max_chars))
    if provider == "groq":
        return max(32, int(config.groq_max_chars))
    return max(32, int(config.max_chars))


def speak_text(text: str, config: TtsConfig) -> None:
    content = str(text or "").strip()
    if not content or not config.enabled:
        return
    chunks = _split_text_for_tts(content, _tts_chunk_limit(config))
    if not chunks:
        return
    for chunk in chunks:
        wav_bytes = synthesize_text(chunk, config)
        play_wav_bytes(wav_bytes, backend=config.playback_backend)


def record_until_enter(config: AudioConfig) -> bytes:
    if sd is None:
        raise RuntimeError(audio_input_unavailable_message())
    chunks: List[np.ndarray] = []
    stop_event = threading.Event()
    device_index, capture_rate, device_name = _resolve_input_stream_settings(
        config.sample_rate,
        config.channels,
        dtype="int16",
    )

    def _callback(indata, frames, _time_info, status) -> None:
        if status or frames <= 0:
            return
        chunks.append(np.array(indata, dtype=np.int16, copy=True))

    def _wait_for_enter() -> None:
        try:
            input()
        except EOFError:
            pass
        stop_event.set()

    print(f"[voice] 录音中，设备={device_name}，按 Enter 结束...", flush=True)
    waiter = threading.Thread(target=_wait_for_enter, daemon=True)
    deadline = time.monotonic() + float(config.max_record_sec)

    with _AUDIO_IO_LOCK:
        stream = sd.InputStream(
            device=device_index,
            samplerate=capture_rate,
            channels=config.channels,
            dtype="int16",
            callback=_callback,
            blocksize=0,
        )
        with stream:
            waiter.start()
            while not stop_event.is_set():
                if time.monotonic() >= deadline:
                    stop_event.set()
                    break
                sd.sleep(50)

    if not chunks:
        return b""
    audio = np.concatenate(chunks, axis=0)
    if capture_rate != config.sample_rate:
        audio = _resample_int16_audio(audio, capture_rate, config.sample_rate)
    with io.BytesIO() as buf:
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(config.channels)
            wf.setsampwidth(2)
            wf.setframerate(config.sample_rate)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()
