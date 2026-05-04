"""Reusable MomoAgent controls for the HMI."""

from __future__ import annotations

import json
import os
import time
from html import escape
from typing import Any, Callable

import requests
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


MOMO_ROBOT_SERVICE_URL_DEFAULT = "http://127.0.0.1:8010"
AGENT_TIMEOUT_SEC_DEFAULT = 90.0


def display_agent_text(text: str) -> str:
    return str(text or "").replace("Nanobot", "MomoAgent").replace("nanobot", "momoagent")


def default_agent_service_url() -> str:
    return (
        str(os.getenv("MOMO_ROBOT_SERVICE_URL", MOMO_ROBOT_SERVICE_URL_DEFAULT)).strip().rstrip("/")
        or MOMO_ROBOT_SERVICE_URL_DEFAULT
    )


def default_agent_timeout_sec() -> float:
    try:
        return max(2.0, float(str(os.getenv("MOMO_ROBOT_AGENT_TIMEOUT_SEC", AGENT_TIMEOUT_SEC_DEFAULT)).strip()))
    except Exception:
        return AGENT_TIMEOUT_SEC_DEFAULT


def agent_status_summary(payload: dict[str, Any]) -> str:
    backend = str(payload.get("backend", "nanobot") or "nanobot").strip()
    busy = bool(payload.get("busy", False))
    enabled = bool(payload.get("enabled", False))
    session_id = str(payload.get("session_id", "") or "").strip()
    agent_key = str(payload.get("agent_session_key", "") or "").strip()
    pieces = [
        f"backend={display_agent_text(backend)}",
        f"enabled={enabled}",
        f"busy={busy}",
    ]
    if session_id:
        pieces.append(f"session={session_id}")
    if agent_key:
        pieces.append(f"agent_key={agent_key}")
    last_error = str(payload.get("last_error", "") or "").strip()
    if last_error:
        pieces.append(f"error={display_agent_text(last_error)}")
    return " | ".join(pieces)


def agent_turn_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    turn = payload.get("turn")
    return turn if isinstance(turn, dict) else {}


def agent_turn_detail(payload: dict[str, Any]) -> str:
    turn = agent_turn_from_payload(payload)
    elapsed = float(turn.get("agent_elapsed_sec", 0.0) or 0.0)
    agent_key = str(turn.get("agent_session_key", "") or "").strip()
    detail = f"Done in {elapsed:.2f}s"
    if agent_key:
        detail += f" | {agent_key}"
    return detail


class AgentRequestWorker(QThread):
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        *,
        mode: str,
        service_url: str,
        message: str = "",
        timeout_sec: float = AGENT_TIMEOUT_SEC_DEFAULT,
    ) -> None:
        super().__init__()
        self._mode = str(mode or "").strip().lower()
        self._service_url = str(service_url or "").strip().rstrip("/") or MOMO_ROBOT_SERVICE_URL_DEFAULT
        self._message = str(message or "").strip()
        self._timeout_sec = max(2.0, float(timeout_sec))

    def _endpoint(self, path: str) -> str:
        return f"{self._service_url}{path}"

    @staticmethod
    def _extract_error(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                code = str(err.get("code", "") or "").strip()
                message = str(err.get("message", "") or "").strip()
                if code and message:
                    return f"{code}: {message}"
                if message:
                    return message
            message = str(payload.get("message", "") or "").strip()
            if message:
                return message
            detail = str(payload.get("detail", "") or "").strip()
            if detail:
                return detail
        return fallback

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = requests.Session()
        session.trust_env = False
        try:
            if method == "GET":
                response = session.get(self._endpoint(path), timeout=self._timeout_sec)
            else:
                response = session.post(
                    self._endpoint(path),
                    json=payload or {},
                    timeout=self._timeout_sec,
                )
        finally:
            session.close()

        try:
            body = response.json()
        except Exception:
            body = None

        if not response.ok:
            raise RuntimeError(self._extract_error(body, f"HTTP {response.status_code}"))
        if not isinstance(body, dict):
            raise RuntimeError("Service returned a non-JSON response")
        if not bool(body.get("ok", False)):
            raise RuntimeError(self._extract_error(body, "Agent request failed"))
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def run(self) -> None:
        try:
            if self._mode == "status":
                data = self._request_json(method="GET", path="/api/v1/agent/status")
            elif self._mode == "warmup":
                prompt = self._message or "请只回复“就绪”。"
                data = self._request_json(
                    method="POST",
                    path="/api/v1/agent/warmup",
                    payload={"prompt": prompt},
                )
            elif self._mode == "ask":
                if not self._message:
                    raise RuntimeError("Prompt is empty")
                data = self._request_json(
                    method="POST",
                    path="/api/v1/agent/ask",
                    payload={"message": self._message},
                )
            else:
                raise RuntimeError(f"Unsupported agent request mode: {self._mode}")
        except Exception as exc:
            self.failed.emit(self._mode, str(exc).strip() or "Agent request failed")
            return
        self.done.emit(self._mode, data)


class AgentSettingsPanel(QWidget):
    line_ready = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._worker: AgentRequestWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self.agent_group = QGroupBox("MomoAgent")
        agent_layout = QVBoxLayout(self.agent_group)
        agent_layout.setSpacing(10)

        form = QFormLayout()
        self.service_url_input = QLineEdit(default_agent_service_url())
        self.service_url_input.setPlaceholderText(MOMO_ROBOT_SERVICE_URL_DEFAULT)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(2.0, 300.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSingleStep(5.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(default_agent_timeout_sec())
        self.status_value = QLabel("Idle")
        self.status_value.setWordWrap(True)

        form.addRow("Service URL", self.service_url_input)
        form.addRow("Timeout", self.timeout_spin)
        form.addRow("Status", self.status_value)
        agent_layout.addLayout(form)

        button_row = QHBoxLayout()
        self.status_btn = QPushButton("Status")
        self.warmup_btn = QPushButton("Warmup")
        button_row.addWidget(self.status_btn)
        button_row.addWidget(self.warmup_btn)
        button_row.addStretch()
        agent_layout.addLayout(button_row)

        root.addWidget(self.agent_group)
        root.addStretch()

        self.status_btn.clicked.connect(lambda: self._start_request("status"))
        self.warmup_btn.clicked.connect(lambda: self._start_request("warmup", "请只回复“就绪”。"))

    def service_url(self) -> str:
        return str(self.service_url_input.text() or "").strip().rstrip("/") or MOMO_ROBOT_SERVICE_URL_DEFAULT

    def timeout_sec(self) -> float:
        return max(2.0, float(self.timeout_spin.value()))

    def set_texts(self, tr) -> None:
        self.agent_group.setTitle(tr("agent_settings_title"))
        self.status_btn.setText(tr("agent_status_btn"))
        self.warmup_btn.setText(tr("agent_warmup_btn"))

    def shutdown(self) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            worker.requestInterruption()
            worker.wait(1000)
            if worker.isRunning():
                worker.terminate()
                worker.wait(500)
        except Exception:
            pass
        self._worker = None

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.service_url_input,
            self.timeout_spin,
            self.status_btn,
            self.warmup_btn,
        ):
            widget.setEnabled(not busy)
        if busy:
            self.status_value.setText("Running...")

    def _start_request(self, mode: str, message: str = "") -> None:
        if self._worker is not None:
            self.line_ready.emit("Error", "Agent request is still running")
            return
        prompt = str(message or "").strip()
        if mode == "warmup" and prompt:
            self.line_ready.emit("You", prompt)
        self._set_busy(True)
        self._worker = AgentRequestWorker(
            mode=mode,
            service_url=self.service_url(),
            message=prompt,
            timeout_sec=self.timeout_sec(),
        )
        self._worker.done.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_done(self, mode: str, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        if mode == "status":
            summary = agent_status_summary(data)
            self.status_value.setText(summary)
            self.line_ready.emit("Status", summary)
            return

        turn = agent_turn_from_payload(data)
        reply = str(turn.get("reply", "") or "").strip()
        if reply:
            self.line_ready.emit("Momo", reply)
        else:
            self.line_ready.emit("Status", json.dumps(data, ensure_ascii=False, indent=2))
        detail = agent_turn_detail(data)
        self.status_value.setText(detail)
        self.line_ready.emit("Status", detail)

    def _on_worker_failed(self, mode: str, message: str) -> None:
        del mode
        msg = str(message or "").strip() or "Agent request failed"
        msg = display_agent_text(msg)
        self.status_value.setText(msg)
        self.line_ready.emit("Error", msg)

    def _on_worker_finished(self) -> None:
        self._worker = None
        if str(self.status_value.text() or "").strip() == "Running...":
            self.status_value.setText("Idle")
        self._set_busy(False)


class AgentChatPanel(QWidget):
    def __init__(
        self,
        *,
        config_provider: Callable[[], tuple[str, float]] | None = None,
    ) -> None:
        super().__init__()
        self._worker: AgentRequestWorker | None = None
        self._config_provider = config_provider or self._default_config
        self._theme = "light"

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        self.chat_group = QGroupBox("Agent Chat")
        chat_layout = QVBoxLayout(self.chat_group)
        chat_layout.setSpacing(10)

        self.history_view = QTextEdit()
        self.history_view.setReadOnly(True)
        self.history_view.setMinimumWidth(320)
        self.history_view.setMinimumHeight(320)
        chat_layout.addWidget(self.history_view, stretch=1)

        self.prompt_input = QTextEdit()
        self.prompt_input.setMinimumHeight(96)
        self.prompt_input.setPlaceholderText("输入要发给 MomoAgent 的测试问题")
        chat_layout.addWidget(self.prompt_input)

        send_row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear")
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primaryBtn")
        send_row.addWidget(self.clear_btn)
        send_row.addStretch()
        send_row.addWidget(self.send_btn)
        chat_layout.addLayout(send_row)

        root.addWidget(self.chat_group, stretch=1)

        self.send_btn.clicked.connect(self._on_send_clicked)
        self.clear_btn.clicked.connect(self.history_view.clear)

    @staticmethod
    def _default_config() -> tuple[str, float]:
        return default_agent_service_url(), default_agent_timeout_sec()

    def set_texts(self, tr) -> None:
        self.chat_group.setTitle(tr("agent_chat_title"))
        self.clear_btn.setText(tr("agent_clear_btn"))
        self.send_btn.setText(tr("agent_send_btn"))
        self.prompt_input.setPlaceholderText(tr("agent_prompt_placeholder"))

    def set_theme(self, theme: str) -> None:
        theme_norm = str(theme or "").strip().lower()
        self._theme = theme_norm if theme_norm in ("light", "dark") else "light"

    def shutdown(self) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            worker.requestInterruption()
            worker.wait(1000)
            if worker.isRunning():
                worker.terminate()
                worker.wait(500)
        except Exception:
            pass
        self._worker = None

    def append_line(self, speaker: str, text: str) -> None:
        label = str(speaker or "").strip()
        body = str(text or "").strip()
        if not body:
            return
        stamp = time.strftime("%H:%M:%S")
        time_color = "#7E93AB" if self._theme == "dark" else "#94A3B8"
        label_colors = {
            "You": "#60A5FA" if self._theme == "dark" else "#2563EB",
            "Momo": "#34D399" if self._theme == "dark" else "#059669",
            "Status": "#FBBF24" if self._theme == "dark" else "#D97706",
            "Error": "#F87171" if self._theme == "dark" else "#DC2626",
        }
        label_color = label_colors.get(label, "#D7E1EE" if self._theme == "dark" else "#1E293B")
        safe_body = escape(display_agent_text(body)).replace("\n", "<br>")
        line = (
            f'<span style="color: {time_color}">[{escape(stamp)}]</span> '
            f'<span style="color: {label_color}; font-weight: 600">{escape(label)}</span>: '
            f'<span>{safe_body}</span>'
        )
        self.history_view.append(line)
        self.history_view.verticalScrollBar().setValue(self.history_view.verticalScrollBar().maximum())

    def append_payload(self, title: str, payload: dict[str, Any]) -> None:
        self.append_line(title, json.dumps(payload, ensure_ascii=False, indent=2))

    def _set_busy(self, busy: bool) -> None:
        self.prompt_input.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)

    def _request_config(self) -> tuple[str, float]:
        try:
            service_url, timeout_sec = self._config_provider()
        except Exception:
            return self._default_config()
        service_url = str(service_url or "").strip().rstrip("/") or MOMO_ROBOT_SERVICE_URL_DEFAULT
        try:
            timeout = max(2.0, float(timeout_sec))
        except Exception:
            timeout = AGENT_TIMEOUT_SEC_DEFAULT
        return service_url, timeout

    def _on_send_clicked(self) -> None:
        prompt = str(self.prompt_input.toPlainText() or "").strip()
        if not prompt:
            self.append_line("Error", "Prompt is empty")
            return
        self.append_line("You", prompt)
        self.prompt_input.clear()
        self._start_request(prompt)

    def _start_request(self, message: str) -> None:
        if self._worker is not None:
            self.append_line("Error", "Agent request is still running")
            return
        service_url, timeout_sec = self._request_config()
        self._set_busy(True)
        self._worker = AgentRequestWorker(
            mode="ask",
            service_url=service_url,
            message=message,
            timeout_sec=timeout_sec,
        )
        self._worker.done.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_done(self, mode: str, payload: object) -> None:
        del mode
        data = payload if isinstance(payload, dict) else {}
        turn = agent_turn_from_payload(data)
        reply = str(turn.get("reply", "") or "").strip()
        if reply:
            self.append_line("Momo", reply)
        else:
            self.append_payload("Status", data)
        self.append_line("Status", agent_turn_detail(data))

    def _on_worker_failed(self, mode: str, message: str) -> None:
        del mode
        self.append_line("Error", display_agent_text(str(message or "").strip() or "Agent request failed"))

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._set_busy(False)
