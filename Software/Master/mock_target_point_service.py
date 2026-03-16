#!/usr/bin/env python3
"""Mock target point service for testing target_center_follow without OpenClaw or a multimodal model."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import cv2  # type: ignore
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _triangle_wave(phase: float) -> float:
    wrapped = phase - math.floor(phase)
    if wrapped < 0.25:
        return wrapped * 4.0
    if wrapped < 0.75:
        return 2.0 - wrapped * 4.0
    return wrapped * 4.0 - 4.0


def _smooth_fraction(fraction: float) -> float:
    t = min(1.0, max(0.0, float(fraction)))
    return t * t * (3.0 - 2.0 * t)


def _compute_offset(center: tuple[float, float], frame_size: tuple[int, int]) -> dict[str, float]:
    width, height = frame_size
    half_width = max(float(width) / 2.0, 1.0)
    half_height = max(float(height) / 2.0, 1.0)
    dx = float(center[0]) - half_width
    dy = float(center[1]) - half_height
    return {
        "dx": round(dx, 3),
        "dy": round(dy, 3),
        "ndx": round(dx / half_width, 6),
        "ndy": round(dy / half_height, 6),
    }


@dataclass(slots=True)
class MockTargetFrame:
    frame_id: int
    timestamp: float
    center: tuple[float, float]
    smoothed_center: tuple[float, float]
    label: str
    reason: str
    source_mode: str
    frame_size: tuple[int, int]

    def to_payload(self) -> dict[str, Any]:
        width, height = self.frame_size
        offset = _compute_offset(self.center, self.frame_size)
        smoothed_offset = _compute_offset(self.smoothed_center, self.frame_size)
        return {
            "ok": True,
            "status": "tracking",
            "running": True,
            "detected": True,
            "frame_id": int(self.frame_id),
            "timestamp": round(float(self.timestamp), 6),
            "frame_size": [int(width), int(height)],
            "target": {
                "center": [round(float(self.center[0]), 3), round(float(self.center[1]), 3)],
                "label": self.label,
                "reason": self.reason,
            },
            "smoothed_target": {
                "center": [round(float(self.smoothed_center[0]), 3), round(float(self.smoothed_center[1]), 3)],
                "label": self.label,
                "reason": self.reason,
            },
            "offset": offset,
            "smoothed_offset": smoothed_offset,
            "mock": {
                "mode": self.source_mode,
                "label": self.label,
                "reason": self.reason,
            },
        }


class MockTargetGenerator:
    def __init__(
        self,
        *,
        width: int,
        height: int,
        mode: str,
        center_x: float,
        center_y: float,
        amplitude_x: float,
        amplitude_y: float,
        period_sec: float,
        radius_x: float,
        radius_y: float,
        smoothing_alpha: float,
        script_entries: list[dict[str, Any]] | None = None,
        loop_script: bool = True,
        shared_state_path: str | None = None,
        base_ndx: float = 0.0,
        base_ndy: float = 0.0,
        pan_ndx_per_rad: float = 1.8,
        tilt_ndy_per_rad: float = 1.0,
        tilt_secondary_ndy_per_rad: float = 0.8,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.mode = str(mode)
        self.center_x = float(center_x)
        self.center_y = float(center_y)
        self.amplitude_x = float(amplitude_x)
        self.amplitude_y = float(amplitude_y)
        self.period_sec = max(0.1, float(period_sec))
        self.radius_x = float(radius_x)
        self.radius_y = float(radius_y)
        self.smoothing_alpha = _clamp(float(smoothing_alpha), 0.0, 1.0)
        self.script_entries = list(script_entries or [])
        self.loop_script = bool(loop_script)
        self.shared_state_path = (
            Path(shared_state_path).expanduser().resolve() if str(shared_state_path or "").strip() else None
        )
        self.base_ndx = float(base_ndx)
        self.base_ndy = float(base_ndy)
        self.pan_ndx_per_rad = float(pan_ndx_per_rad)
        self.tilt_ndy_per_rad = float(tilt_ndy_per_rad)
        self.tilt_secondary_ndy_per_rad = float(tilt_secondary_ndy_per_rad)

        self._lock = Lock()
        self._started_at = time.time()
        self._frame_id = 0
        self._smoothed_center = (float(self.center_x), float(self.center_y))
        self._zero_q: Optional[tuple[float, ...]] = None

    def _elapsed(self, now: float) -> float:
        return max(0.0, float(now) - float(self._started_at))

    def _mode_fixed(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        _ = elapsed
        return (self.center_x, self.center_y), "mock_fixed_target", "fixed mock target"

    def _mode_horizontal_sweep(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        phase = elapsed / self.period_sec
        x = self.center_x + self.amplitude_x * math.sin(phase * 2.0 * math.pi)
        y = self.center_y
        return (x, y), "mock_sweep_target", "horizontal sweep mock target"

    def _mode_vertical_sweep(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        phase = elapsed / self.period_sec
        x = self.center_x
        y = self.center_y + self.amplitude_y * math.sin(phase * 2.0 * math.pi)
        return (x, y), "mock_vertical_target", "vertical sweep mock target"

    def _mode_circle(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        phase = elapsed / self.period_sec
        angle = phase * 2.0 * math.pi
        x = self.center_x + self.radius_x * math.cos(angle)
        y = self.center_y + self.radius_y * math.sin(angle)
        return (x, y), "mock_circle_target", "circle mock target"

    def _mode_corners(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        corners = [
            (self.center_x - self.amplitude_x, self.center_y - self.amplitude_y),
            (self.center_x + self.amplitude_x, self.center_y - self.amplitude_y),
            (self.center_x + self.amplitude_x, self.center_y + self.amplitude_y),
            (self.center_x - self.amplitude_x, self.center_y + self.amplitude_y),
        ]
        slot = max(self.period_sec / 4.0, 0.1)
        index = int(elapsed / slot) % len(corners)
        return corners[index], "mock_corner_target", "corner stepping mock target"

    def _mode_triangle(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        phase = elapsed / self.period_sec
        x = self.center_x + self.amplitude_x * _triangle_wave(phase)
        y = self.center_y + self.amplitude_y * _triangle_wave(phase + 0.25)
        return (x, y), "mock_triangle_target", "triangle-wave mock target"

    def _mode_scripted(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        if not self.script_entries:
            return self._mode_fixed(elapsed)

        total_duration = 0.0
        normalized_entries: list[tuple[float, dict[str, Any]]] = []
        for entry in self.script_entries:
            duration = max(0.1, float(entry.get("duration_sec", 1.0)))
            total_duration += duration
            normalized_entries.append((duration, entry))

        timeline = elapsed
        if self.loop_script and total_duration > 0.0:
            timeline = timeline % total_duration
        elif timeline >= total_duration:
            duration, last_entry = normalized_entries[-1]
            _ = duration
            center = self._extract_entry_center(last_entry)
            return center, str(last_entry.get("label", "mock_script_target")), str(
                last_entry.get("reason", "scripted mock target")
            )

        cursor = 0.0
        for duration, entry in normalized_entries:
            if timeline < cursor + duration:
                center = self._extract_entry_center(entry)
                return center, str(entry.get("label", "mock_script_target")), str(
                    entry.get("reason", "scripted mock target")
                )
            cursor += duration
        center = self._extract_entry_center(normalized_entries[-1][1])
        return center, "mock_script_target", "scripted mock target"

    def _default_shared_state(self) -> dict[str, Any]:
        return {
            "q_start": [0.0] * 5,
            "q_target": [0.0] * 5,
            "motion_start_time": 0.0,
            "motion_end_time": 0.0,
        }

    def _read_shared_current_q(self, now: float) -> tuple[float, ...]:
        if self.shared_state_path is None:
            raise ValueError("shared state path is required for arm-feedback mode")
        if not self.shared_state_path.exists():
            state = self._default_shared_state()
        else:
            raw = self.shared_state_path.read_text(encoding="utf-8").strip()
            if not raw:
                state = self._default_shared_state()
            else:
                try:
                    state = json.loads(raw)
                except json.JSONDecodeError:
                    state = self._default_shared_state()

        q_start = state.get("q_start", [0.0] * 5)
        q_target = state.get("q_target", [0.0] * 5)
        if not isinstance(q_start, list) or len(q_start) < 5:
            q_start = [0.0] * 5
        if not isinstance(q_target, list) or len(q_target) < 5:
            q_target = [0.0] * 5
        motion_start_time = float(state.get("motion_start_time", 0.0))
        motion_end_time = float(state.get("motion_end_time", 0.0))
        start = [float(x) for x in q_start[:5]]
        target = [float(x) for x in q_target[:5]]

        if motion_end_time <= motion_start_time or now >= motion_end_time:
            return tuple(target)
        if now <= motion_start_time:
            return tuple(start)
        span = max(1e-9, motion_end_time - motion_start_time)
        alpha = _smooth_fraction((now - motion_start_time) / span)
        return tuple(float(s + (t - s) * alpha) for s, t in zip(start, target))

    def _mode_arm_feedback(self, elapsed: float) -> tuple[tuple[float, float], str, str]:
        _ = elapsed
        now = time.time()
        current_q = self._read_shared_current_q(now)
        if self._zero_q is None:
            self._zero_q = tuple(current_q)
        q0 = self._zero_q
        ndx = self.base_ndx - self.pan_ndx_per_rad * (float(current_q[0]) - float(q0[0]))
        ndy = (
            self.base_ndy
            + self.tilt_ndy_per_rad * (float(current_q[1]) - float(q0[1]))
            + self.tilt_secondary_ndy_per_rad * (float(current_q[2]) - float(q0[2]))
        )
        x = (float(self.width) / 2.0) * (1.0 + ndx)
        y = (float(self.height) / 2.0) * (1.0 + ndy)
        return (x, y), "mock_feedback_target", "mock target reacts to mock arm state"

    def _extract_entry_center(self, entry: dict[str, Any]) -> tuple[float, float]:
        raw = entry.get("center")
        if isinstance(raw, dict):
            x = float(raw.get("x", self.center_x))
            y = float(raw.get("y", self.center_y))
            return x, y
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return float(raw[0]), float(raw[1])
        raise ValueError(f"Script entry center must be [x, y] or {{x, y}}: {entry}")

    def _compute_center(self, now: float) -> tuple[tuple[float, float], str, str]:
        elapsed = self._elapsed(now)
        if self.mode == "fixed":
            center, label, reason = self._mode_fixed(elapsed)
        elif self.mode == "horizontal-sweep":
            center, label, reason = self._mode_horizontal_sweep(elapsed)
        elif self.mode == "vertical-sweep":
            center, label, reason = self._mode_vertical_sweep(elapsed)
        elif self.mode == "circle":
            center, label, reason = self._mode_circle(elapsed)
        elif self.mode == "corners":
            center, label, reason = self._mode_corners(elapsed)
        elif self.mode == "triangle":
            center, label, reason = self._mode_triangle(elapsed)
        elif self.mode == "scripted":
            center, label, reason = self._mode_scripted(elapsed)
        elif self.mode == "arm-feedback":
            center, label, reason = self._mode_arm_feedback(elapsed)
        else:
            raise ValueError(f"unsupported mode: {self.mode}")

        x = _clamp(float(center[0]), 0.0, float(self.width))
        y = _clamp(float(center[1]), 0.0, float(self.height))
        return (x, y), label, reason

    def latest(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._frame_id += 1
            center, label, reason = self._compute_center(now)
            last_x, last_y = self._smoothed_center
            smoothed_x = last_x + (center[0] - last_x) * self.smoothing_alpha
            smoothed_y = last_y + (center[1] - last_y) * self.smoothing_alpha
            self._smoothed_center = (smoothed_x, smoothed_y)
            frame = MockTargetFrame(
                frame_id=self._frame_id,
                timestamp=now,
                center=center,
                smoothed_center=self._smoothed_center,
                label=label,
                reason=reason,
                source_mode=self.mode,
                frame_size=(self.width, self.height),
            )
        return frame.to_payload()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "running": True,
                "mode": self.mode,
                "frame_size": [self.width, self.height],
                "frame_id": self._frame_id,
                "started_at": round(self._started_at, 6),
                "uptime_sec": round(max(0.0, time.time() - self._started_at), 3),
                "script_entries": len(self.script_entries),
            }


class MockTargetService:
    def __init__(self, generator: MockTargetGenerator) -> None:
        self.generator = generator

    def create_handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    payload = {"status": "ok", "running": True, "mode": service.generator.mode}
                    self._write_json(200, payload)
                    return
                if parsed.path == "/status":
                    self._write_json(200, service.generator.status())
                    return
                if parsed.path == "/latest":
                    self._write_json(200, service.generator.latest())
                    return
                self._write_json(404, {"ok": False, "error": f"unknown path: {parsed.path}"})

            def log_message(self, format: str, *args: Any) -> None:
                _ = format, args

            def _write_json(self, code: int, payload: dict[str, Any]) -> None:
                blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(code))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        return Handler


def _render_preview(payload: dict[str, Any], background_frame: "np.ndarray | None" = None) -> "np.ndarray":
    if np is None or cv2 is None:
        raise RuntimeError("OpenCV preview is unavailable")

    if background_frame is not None and isinstance(background_frame, np.ndarray) and background_frame.size > 0:
        frame = background_frame.copy()
        height, width = frame.shape[:2]
    else:
        frame_size = payload.get("frame_size") or [1280, 720]
        width = int(frame_size[0]) if isinstance(frame_size, list) and len(frame_size) >= 2 else 1280
        height = int(frame_size[1]) if isinstance(frame_size, list) and len(frame_size) >= 2 else 720
        frame = np.full((height, width, 3), 246, dtype=np.uint8)

    cx = width // 2
    cy = height // 2
    cv2.line(frame, (cx, 0), (cx, height - 1), (200, 200, 200), 1)
    cv2.line(frame, (0, cy), (width - 1, cy), (200, 200, 200), 1)

    target = (payload.get("target") or {})
    smoothed_target = (payload.get("smoothed_target") or {})
    target_center = target.get("center") if isinstance(target, dict) else None
    smoothed_center = smoothed_target.get("center") if isinstance(smoothed_target, dict) else None

    if isinstance(target_center, list) and len(target_center) >= 2:
        point = (int(round(float(target_center[0]))), int(round(float(target_center[1]))))
        cv2.circle(frame, point, 12, (50, 80, 235), -1)
        cv2.circle(frame, point, 20, (50, 80, 235), 2)

    if isinstance(smoothed_center, list) and len(smoothed_center) >= 2:
        point = (int(round(float(smoothed_center[0]))), int(round(float(smoothed_center[1]))))
        cv2.circle(frame, point, 8, (60, 190, 90), -1)
        cv2.circle(frame, point, 14, (60, 190, 90), 2)

    label = str((payload.get("target") or {}).get("label", "")).strip()
    reason = str((payload.get("target") or {}).get("reason", "")).strip()
    mode = str((payload.get("mock") or {}).get("mode", "")).strip()
    offset = payload.get("smoothed_offset") or payload.get("offset") or {}
    ndx = float(offset.get("ndx", 0.0)) if isinstance(offset, dict) else 0.0
    ndy = float(offset.get("ndy", 0.0)) if isinstance(offset, dict) else 0.0

    cv2.putText(frame, f"mode={mode}", (18, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(
        frame,
        f"frame={int(payload.get('frame_id', 0))} ndx={ndx:+.3f} ndy={ndy:+.3f}",
        (18, 66),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (40, 40, 40),
        2,
        cv2.LINE_AA,
    )
    if label:
        cv2.putText(frame, f"label={label}", (18, 98), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 40), 2, cv2.LINE_AA)
    if reason:
        cv2.putText(frame, reason[:80], (18, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (90, 90, 90), 1, cv2.LINE_AA)
    return frame


def _load_script_entries(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    script_path = Path(path).expanduser().resolve()
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"script must be a JSON array: {script_path}")
    normalized: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError(f"each script entry must be an object: {entry!r}")
        normalized.append(dict(entry))
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mock target point service for target_center_follow")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument(
        "--mode",
        default="horizontal-sweep",
        choices=("fixed", "horizontal-sweep", "vertical-sweep", "circle", "corners", "triangle", "scripted", "arm-feedback"),
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--center-x", type=float, default=None)
    parser.add_argument("--center-y", type=float, default=None)
    parser.add_argument("--amplitude-x", type=float, default=260.0)
    parser.add_argument("--amplitude-y", type=float, default=140.0)
    parser.add_argument("--period-sec", type=float, default=6.0)
    parser.add_argument("--radius-x", type=float, default=220.0)
    parser.add_argument("--radius-y", type=float, default=120.0)
    parser.add_argument("--smoothing-alpha", type=float, default=0.35)
    parser.add_argument("--show-window", action="store_true", help="Render a preview window with crosshair and target points")
    parser.add_argument("--window-name", default="Mock Target Point Service")
    parser.add_argument("--preview-fps", type=float, default=20.0)
    parser.add_argument("--camera-device", default="", help="Optional V4L2 device path, for example /dev/video0")
    parser.add_argument("--camera-name-hint", default="", help="Optional V4L2 name hint when selecting a camera")
    parser.add_argument("--camera-video-index", type=int, default=0, help="Index within a V4L2 device block")
    parser.add_argument("--camera-width", type=int, default=1280)
    parser.add_argument("--camera-height", type=int, default=720)
    parser.add_argument("--camera-fps", type=int, default=30)
    parser.add_argument("--camera-rotate", type=int, default=0)
    parser.add_argument("--camera-pixel-format", default="", help="Optional 4-char V4L2 pixel format, for example MJPG")
    parser.add_argument("--script", default="", help="Optional JSON array script for --mode scripted")
    parser.add_argument(
        "--shared-state",
        default=str(os.getenv("SOARMMOCE_MOCK_SHARED_STATE_FILE", "")).strip(),
        help="Optional SOARMMOCE_MOCK_SHARED_STATE_FILE path for --mode arm-feedback",
    )
    parser.add_argument("--base-ndx", type=float, default=0.25, help="Initial normalized x offset for --mode arm-feedback")
    parser.add_argument("--base-ndy", type=float, default=0.12, help="Initial normalized y offset for --mode arm-feedback")
    parser.add_argument("--pan-ndx-per-rad", type=float, default=1.8)
    parser.add_argument("--tilt-ndy-per-rad", type=float, default=1.0)
    parser.add_argument("--tilt-secondary-ndy-per-rad", type=float, default=0.8)
    parser.add_argument(
        "--loop-script",
        type=lambda raw: str(raw).strip().lower() not in {"0", "false", "no"},
        default=True,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    width = max(2, int(args.width))
    height = max(2, int(args.height))
    center_x = float(args.center_x) if args.center_x is not None else float(width) / 2.0
    center_y = float(args.center_y) if args.center_y is not None else float(height) / 2.0

    generator = MockTargetGenerator(
        width=width,
        height=height,
        mode=str(args.mode),
        center_x=center_x,
        center_y=center_y,
        amplitude_x=float(args.amplitude_x),
        amplitude_y=float(args.amplitude_y),
        period_sec=float(args.period_sec),
        radius_x=float(args.radius_x),
        radius_y=float(args.radius_y),
        smoothing_alpha=float(args.smoothing_alpha),
        script_entries=_load_script_entries(args.script),
        loop_script=bool(args.loop_script),
        shared_state_path=args.shared_state,
        base_ndx=float(args.base_ndx),
        base_ndy=float(args.base_ndy),
        pan_ndx_per_rad=float(args.pan_ndx_per_rad),
        tilt_ndy_per_rad=float(args.tilt_ndy_per_rad),
        tilt_secondary_ndy_per_rad=float(args.tilt_secondary_ndy_per_rad),
    )
    service = MockTargetService(generator)
    server = ThreadingHTTPServer((str(args.host), int(args.port)), service.create_handler())
    camera_reader = None
    print(
        f"[mock-target-point-service] listening on http://{args.host}:{args.port} "
        f"mode={args.mode} frame_size={width}x{height}",
        flush=True,
    )
    try:
        if not bool(args.show_window):
            server.serve_forever()
            return
        if cv2 is None or np is None:
            raise RuntimeError("OpenCV preview requested but cv2/numpy are unavailable")
        if str(args.camera_device or "").strip() or str(args.camera_name_hint or "").strip():
            from v4l2_camera_reader import V4L2CameraReader

            camera_reader = V4L2CameraReader(
                device=str(args.camera_device or "").strip() or None,
                name_hint=str(args.camera_name_hint or "").strip() or None,
                preferred_video_index=int(args.camera_video_index),
                width=int(args.camera_width),
                height=int(args.camera_height),
                fps=int(args.camera_fps),
                pixel_format=str(args.camera_pixel_format or "").strip() or None,
                rotation_deg=int(args.camera_rotate),
            )
            camera_reader.start()
            first_frame = camera_reader.read(timeout_sec=3.0, copy=True)
            if first_frame is not None:
                frame_height, frame_width = first_frame.shape[:2]
                generator.width = int(frame_width)
                generator.height = int(frame_height)
                if args.center_x is None:
                    generator.center_x = float(frame_width) / 2.0
                if args.center_y is None:
                    generator.center_y = float(frame_height) / 2.0
                generator._smoothed_center = (float(generator.center_x), float(generator.center_y))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        frame_interval = 1.0 / max(1.0, float(args.preview_fps))
        while True:
            payload = generator.latest()
            background_frame = None
            if camera_reader is not None:
                background_frame, _, _ = camera_reader.get_latest(copy=True)
            frame = _render_preview(payload, background_frame=background_frame)
            cv2.imshow(str(args.window_name), frame)
            key = cv2.waitKey(max(1, int(frame_interval * 1000.0))) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
    except KeyboardInterrupt:
        pass
    finally:
        if camera_reader is not None:
            camera_reader.stop()
        server.server_close()
        if cv2 is not None:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
