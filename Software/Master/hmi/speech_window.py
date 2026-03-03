"""Floating speech input window with recording + Groq STT loop."""

from __future__ import annotations

import io
import json
import os
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests
import sounddevice as sd
from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget

# Extracted for easy replacement with env var later.
# Recommended: export GROQ_API_KEY and remove fallback literal.
GROQ_API_KEY_FALLBACK = " "
GROQ_STT_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL_DEFAULT = "whisper-large-v3"

OPENCLAW_BIN_DEFAULT = "openclaw"
OPENCLAW_AGENT_ID_DEFAULT = "main"
OPENCLAW_TIMEOUT_SEC_DEFAULT = 90.0
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

    def __init__(
        self,
        message: str,
        openclaw_bin: str,
        agent_id: str,
        session_id: str,
        local_mode: bool,
        timeout_sec: float,
        thinking: str,
    ):
        super().__init__()
        self._message = str(message or "").strip()
        self._openclaw_bin = str(openclaw_bin or OPENCLAW_BIN_DEFAULT).strip() or OPENCLAW_BIN_DEFAULT
        self._agent_id = str(agent_id or OPENCLAW_AGENT_ID_DEFAULT).strip() or OPENCLAW_AGENT_ID_DEFAULT
        self._session_id = str(session_id or "").strip()
        self._local_mode = bool(local_mode)
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._thinking = str(thinking or OPENCLAW_THINKING_DEFAULT).strip() or OPENCLAW_THINKING_DEFAULT

    def run(self):
        if not self._message:
            self.failed.emit("OpenClaw 输入为空")
            return

        cmd = [
            self._openclaw_bin,
            "--no-color",
            "agent",
            "--json",
            "--message",
            self._message,
            "--thinking",
            self._thinking,
        ]
        if self._local_mode:
            cmd.append("--local")

        if self._session_id:
            cmd.extend(["--session-id", self._session_id])
        else:
            cmd.extend(["--agent", self._agent_id])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
                check=False,
            )
        except FileNotFoundError:
            self.failed.emit(f"找不到 OpenClaw 可执行文件: {self._openclaw_bin}")
            return
        except subprocess.TimeoutExpired:
            self.failed.emit("OpenClaw 调用超时")
            return
        except Exception as exc:
            self.failed.emit(f"OpenClaw 调用失败: {exc}")
            return

        stdout_text = str(proc.stdout or "").strip()
        stderr_text = str(proc.stderr or "").strip()

        if proc.returncode != 0:
            err = stderr_text or stdout_text or f"OpenClaw 返回错误码 {proc.returncode}"
            self.failed.emit(err)
            return

        payload = _parse_json_with_noise(stdout_text)
        if payload is None and stderr_text:
            payload = _parse_json_with_noise(stderr_text)

        session_id = _extract_openclaw_session_id(payload)
        reply = _extract_text_from_openclaw_payload(payload)
        if not reply:
            reply = stdout_text
        reply = str(reply or "").strip()
        if not reply:
            self.failed.emit("OpenClaw 未返回可用文本")
            return

        self.done.emit(reply, session_id)


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

        if icon_path is not None:
            self.set_icon(icon_path)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_timer.start()

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

    # Backward compatibility for old call sites.
    def set_minimax_config(self, api_key: str, stt_url: Optional[str] = None, model: Optional[str] = None):
        self.set_groq_config(api_key=api_key, stt_url=stt_url, model=model)

    def _on_anim_tick(self):
        self._phase = (self._phase + self._anim_timer.interval() / self._cycle_ms) % 1.0
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
        self.update()

    def _stop_listening(self):
        if not self._is_listening:
            return b""
        self._is_listening = False
        self.listening_changed.emit(False)

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
        )
        self._openclaw_worker.done.connect(self._on_openclaw_done)
        self._openclaw_worker.failed.connect(self._on_openclaw_failed)
        self._openclaw_worker.finished.connect(self._on_openclaw_finished)
        self._openclaw_worker.start()

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

        amp = 1.20 if self._is_listening else 0.45
        if self._is_transcribing:
            amp = 0.65
        if self._is_agent_running:
            amp = max(amp, 0.78)
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
