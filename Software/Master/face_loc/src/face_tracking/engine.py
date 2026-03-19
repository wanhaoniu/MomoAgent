from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Any

import numpy as np

from face_tracking.config import AppConfig
from face_tracking.controller import MirrorFollowControllerHint
from face_tracking.detectors import create_detector
from face_tracking.result_store import ResultStore
from face_tracking.schemas import FramePacket, compute_offset_payload, zero_offset_payload
from face_tracking.selection import TargetSelector
from face_tracking.smoothing import FaceTrackerSmoother
from face_tracking.video_source import VideoSource
from face_tracking.visualizer import FrameVisualizer


class RollingFps:
    def __init__(self, window_size: int = 30) -> None:
        self._timestamps: deque[float] = deque(maxlen=window_size)
        self._fps = 0.0

    def tick(self, timestamp: float | None = None) -> float:
        now = timestamp or time.time()
        self._timestamps.append(now)
        if len(self._timestamps) >= 2:
            elapsed = self._timestamps[-1] - self._timestamps[0]
            if elapsed > 0:
                self._fps = (len(self._timestamps) - 1) / elapsed
        return self._fps

    @property
    def current(self) -> float:
        return self._fps


class TrackingEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        self.result_store = ResultStore()

        self.source = VideoSource(config.source, self.logger)
        self.detector = create_detector(config.detector, self.logger)
        self.selector = TargetSelector(config.selection)
        self.smoother = FaceTrackerSmoother(config.smoothing)
        self.controller = MirrorFollowControllerHint(config.hint)
        self.visualizer = FrameVisualizer(config.visualizer) if config.visualizer.enabled else None

        self._stop_event = threading.Event()
        self._frame_queue: queue.Queue[FramePacket] = queue.Queue(maxsize=config.runtime.frame_queue_size)
        self._capture_thread: threading.Thread | None = None
        self._process_thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._display_lock = threading.Lock()
        self._running = False
        self._last_error: str | None = None
        self._frame_counter = 0
        self._fps = RollingFps(window_size=config.runtime.fps_window_size)
        self._started_at: float | None = None
        self._latest_display_frame: np.ndarray | None = None
        self._latest_display_frame_id = 0

        self.result_store.publish(self._build_empty_result(status="starting"))

    def start(self) -> None:
        with self._status_lock:
            if self._running:
                return

        self.logger.info("Starting tracking engine")
        self._stop_event.clear()
        self._started_at = time.time()
        try:
            self.source.open()
            self.detector.initialize()
        except Exception:
            self._cleanup_resources()
            raise

        self._capture_thread = threading.Thread(target=self._capture_loop, name="capture-thread", daemon=True)
        self._process_thread = threading.Thread(target=self._process_loop, name="process-thread", daemon=True)
        self._capture_thread.start()
        self._process_thread.start()

        with self._status_lock:
            self._running = True
            self._last_error = None

    def stop(self) -> None:
        self.logger.info("Stopping tracking engine")
        self._stop_event.set()
        for thread in [self._capture_thread, self._process_thread]:
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._cleanup_resources()
        with self._status_lock:
            self._running = False
        self.result_store.publish(self._build_empty_result(status="stopped"))

    def _cleanup_resources(self) -> None:
        self.source.release()
        self.detector.close()
        if self.visualizer:
            self.visualizer.close()
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ok, frame = self.source.read()
            except Exception as exc:
                ok, frame = False, None
                self._last_error = f"Exception while reading frame: {exc}"
                self.logger.exception("Unexpected video source exception")

            if not ok or frame is None:
                self._last_error = "Failed to read frame from source"
                self.logger.warning("Frame read failed, trying to reconnect in %.1f sec", self.config.source.reconnect_interval_sec)
                self.result_store.publish(self._build_empty_result(status="source_error", error=self._last_error))
                time.sleep(self.config.source.reconnect_interval_sec)
                try:
                    self.source.reconnect()
                    self._last_error = None
                except Exception as exc:
                    self._last_error = f"Reconnect failed: {exc}"
                    self.logger.error(self._last_error)
                continue

            self._frame_counter += 1
            packet = FramePacket(frame_id=self._frame_counter, timestamp=time.time(), frame=frame)
            self._offer_frame(packet)

    def _offer_frame(self, packet: FramePacket) -> None:
        try:
            self._frame_queue.put_nowait(packet)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            self._frame_queue.put_nowait(packet)

    def _process_loop(self) -> None:
        while not self._stop_event.is_set() or not self._frame_queue.empty():
            try:
                packet = self._frame_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                result = self._process_packet(packet)
            except Exception as exc:
                self.logger.exception("Processing failure")
                result = self._build_empty_result(
                    status="processing_error",
                    frame_id=packet.frame_id,
                    timestamp=packet.timestamp,
                    frame_size=(packet.frame.shape[1], packet.frame.shape[0]),
                    fps=self._fps.current,
                    error=str(exc),
                )

            self.result_store.publish(result)
            if self.visualizer:
                annotated = self.visualizer.draw(packet.frame.copy(), result)
                self._publish_display_frame(packet.frame_id, annotated)

    def _process_packet(self, packet: FramePacket) -> dict[str, Any]:
        frame_height, frame_width = packet.frame.shape[:2]
        frame_size = (frame_width, frame_height)
        detections = self.detector.detect(packet.frame)
        target = self.selector.select(detections, packet.frame.shape)
        fps = self._fps.tick(packet.timestamp)

        if target is None:
            self.smoother.on_miss()
            return self._build_empty_result(
                status="no_face",
                frame_id=packet.frame_id,
                timestamp=packet.timestamp,
                frame_size=frame_size,
                fps=fps,
                faces_detected=len(detections),
            )

        raw_payload = target.to_payload(frame_size)
        raw_area_ratio = float(raw_payload["area_ratio"])
        smoothed_state = self.smoother.update(tuple(raw_payload["center"]), raw_area_ratio)
        raw_offset = compute_offset_payload(tuple(raw_payload["center"]), frame_size)
        smoothed_offset = compute_offset_payload(smoothed_state.center, frame_size)
        hint_payload = self.controller.compute(
            raw_offset=raw_offset,
            smoothed_offset=smoothed_offset,
            raw_area_ratio=raw_area_ratio,
            smoothed_area_ratio=smoothed_state.area_ratio,
            detected=True,
        )

        return {
            "timestamp": round(packet.timestamp, 6),
            "frame_id": packet.frame_id,
            "status": "tracking",
            "detected": True,
            "faces_detected": len(detections),
            "target_selection_strategy": self.selector.strategy_name,
            "target_face": raw_payload,
            "smoothed_target_face": {
                "center": [round(smoothed_state.center[0], 3), round(smoothed_state.center[1], 3)],
                "area_ratio": round(smoothed_state.area_ratio, 6),
            },
            "offset": raw_offset,
            "smoothed_offset": smoothed_offset,
            "distance_hint": hint_payload["distance_hint"],
            "lateral_hint": hint_payload["lateral_hint"],
            "vertical_hint": hint_payload["vertical_hint"],
            "combined_hint": hint_payload["combined_hint"],
            "fps": round(fps, 3),
            "frame_size": [frame_width, frame_height],
            "detector_backend": self.detector.backend_name,
            "detector_device": self.detector.describe().get("device"),
            "video_source": self.source.describe(),
            "error": None,
        }

    def _build_empty_result(
        self,
        status: str,
        frame_id: int = 0,
        timestamp: float | None = None,
        frame_size: tuple[int, int] = (0, 0),
        fps: float = 0.0,
        faces_detected: int = 0,
        error: str | None = None,
    ) -> dict[str, Any]:
        timestamp = timestamp or time.time()
        hint_payload = self.controller.compute(
            raw_offset=zero_offset_payload(),
            smoothed_offset=zero_offset_payload(),
            raw_area_ratio=0.0,
            smoothed_area_ratio=0.0,
            detected=False,
        )
        return {
            "timestamp": round(timestamp, 6),
            "frame_id": frame_id,
            "status": status,
            "detected": False,
            "faces_detected": faces_detected,
            "target_selection_strategy": self.selector.strategy_name,
            "target_face": None,
            "smoothed_target_face": None,
            "offset": zero_offset_payload(),
            "smoothed_offset": zero_offset_payload(),
            "distance_hint": hint_payload["distance_hint"],
            "lateral_hint": hint_payload["lateral_hint"],
            "vertical_hint": hint_payload["vertical_hint"],
            "combined_hint": hint_payload["combined_hint"],
            "fps": round(float(fps), 3),
            "frame_size": [frame_size[0], frame_size[1]],
            "detector_backend": self.detector.backend_name,
            "detector_device": self.detector.describe().get("device"),
            "video_source": self.source.describe(),
            "error": error,
        }

    def get_latest_result(self) -> dict[str, Any]:
        latest, _ = self.result_store.get_latest()
        return latest or self._build_empty_result(status="starting")

    def wait_for_newer_result(self, last_version: int, timeout: float) -> tuple[dict[str, Any], int]:
        latest, version = self.result_store.wait_for_newer(last_version, timeout)
        if latest is None:
            latest = self._build_empty_result(status="starting")
        return latest, version

    def get_status(self) -> dict[str, Any]:
        latest, version = self.result_store.get_latest()
        with self._status_lock:
            running = self._running and not self._stop_event.is_set()
        return {
            "running": running,
            "pid": os.getpid(),
            "uptime_sec": None if self._started_at is None else round(time.time() - self._started_at, 3),
            "last_error": self._last_error,
            "fps": 0.0 if latest is None else latest.get("fps", 0.0),
            "latest_frame_id": None if latest is None else latest.get("frame_id"),
            "latest_status": None if latest is None else latest.get("status"),
            "faces_detected": None if latest is None else latest.get("faces_detected"),
            "store_version": version,
            "visualizer_enabled": self.config.visualizer.enabled,
            "source": self.source.describe(),
            "detector": self.detector.describe(),
        }

    def _publish_display_frame(self, frame_id: int, frame: np.ndarray) -> None:
        with self._display_lock:
            self._latest_display_frame = frame
            self._latest_display_frame_id = frame_id

    def get_latest_display_frame(self) -> tuple[np.ndarray | None, int]:
        with self._display_lock:
            return self._latest_display_frame, self._latest_display_frame_id

    def run_visualizer_loop(self, poll_interval_sec: float = 0.02) -> None:
        if not self.visualizer:
            return

        last_frame_id = -1
        current_frame: np.ndarray | None = None

        try:
            while not self._stop_event.is_set():
                frame, frame_id = self.get_latest_display_frame()
                if frame is not None and frame_id != last_frame_id:
                    current_frame = frame
                    last_frame_id = frame_id

                if current_frame is not None:
                    keep_running = self.visualizer.show(current_frame)
                    if not keep_running:
                        self._stop_event.set()
                        break
                time.sleep(poll_interval_sec)
        finally:
            self.visualizer.close()
