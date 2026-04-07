from __future__ import annotations

import logging
import multiprocessing as mproc
import queue
import time
import urllib.request
from collections import deque
from pathlib import Path
from threading import Event, Thread
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

from gesture_tracking.config import AppConfig
from gesture_tracking.result_store import ResultStore
from gesture_tracking.source import OpenCvFrameSource


LOGGER = logging.getLogger(__name__)

# Keep overlay drawing independent from MediaPipe's optional solutions package.
HAND_CONNECTIONS = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 4),
    (0, 5),
    (5, 6),
    (6, 7),
    (7, 8),
    (5, 9),
    (9, 10),
    (10, 11),
    (11, 12),
    (9, 13),
    (13, 14),
    (14, 15),
    (15, 16),
    (13, 17),
    (0, 17),
    (17, 18),
    (18, 19),
    (19, 20),
)


class FpsMeter:
    def __init__(self, window_size: int = 30) -> None:
        self._timestamps: deque[float] = deque(maxlen=max(2, int(window_size)))

    def tick(self, timestamp: float) -> float:
        self._timestamps.append(float(timestamp))
        if len(self._timestamps) < 2:
            return 0.0
        duration = self._timestamps[-1] - self._timestamps[0]
        if duration <= 1e-6:
            return 0.0
        return (len(self._timestamps) - 1) / duration


class GestureStabilizer:
    def __init__(self, stability_frames: int, missing_reset_frames: int) -> None:
        self._stability_frames = int(stability_frames)
        self._missing_reset_frames = int(missing_reset_frames)
        self._candidate_name: str | None = None
        self._candidate_count = 0
        self._stable_name: str | None = None
        self._stable_count = 0
        self._missing_count = 0

    def update(self, gesture_name: str | None) -> tuple[str | None, int]:
        if not gesture_name:
            self._missing_count += 1
            self._candidate_name = None
            self._candidate_count = 0
            if self._missing_count >= self._missing_reset_frames:
                self._stable_name = None
                self._stable_count = 0
            return self._stable_name, self._stable_count

        self._missing_count = 0
        if gesture_name == self._candidate_name:
            self._candidate_count += 1
        else:
            self._candidate_name = gesture_name
            self._candidate_count = 1

        if self._candidate_count >= self._stability_frames:
            if gesture_name == self._stable_name:
                self._stable_count += 1
            else:
                self._stable_name = gesture_name
                self._stable_count = self._candidate_count
        return self._stable_name, self._stable_count


def _visualizer_process_main(frame_queue: Any, window_name: str) -> None:
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
    try:
        while True:
            try:
                kind, payload = frame_queue.get(timeout=0.05)
            except queue.Empty:
                cv2.waitKey(1)
                continue

            if kind == "stop":
                break
            if kind != "frame":
                continue

            cv2.imshow(window_name, payload)
            cv2.waitKey(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cv2.destroyWindow(window_name)
        except Exception:
            pass


class OpenCvVisualizer:
    def __init__(self, enabled: bool, window_name: str) -> None:
        self._enabled = bool(enabled)
        self._window_name = str(window_name)
        self._queue: Any | None = None
        self._process: mproc.Process | None = None

    def start(self) -> None:
        if not self._enabled:
            return
        if self._process and self._process.is_alive():
            return
        ctx = mproc.get_context("spawn")
        self._queue = ctx.Queue(maxsize=1)
        self._process = ctx.Process(
            target=_visualizer_process_main,
            args=(self._queue, self._window_name),
            name="gesture-visualizer",
            daemon=True,
        )
        self._process.start()

    def publish(self, frame: np.ndarray) -> None:
        if not self._enabled or self._queue is None:
            return
        if self._process is not None and not self._process.is_alive():
            return

        message = ("frame", frame)
        try:
            self._queue.put_nowait(message)
            return
        except queue.Full:
            pass
        except (EOFError, OSError, ValueError):
            return

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        except (EOFError, OSError, ValueError):
            return

        try:
            self._queue.put_nowait(message)
        except queue.Full:
            pass
        except (EOFError, OSError, ValueError):
            return

    def stop(self) -> None:
        if self._queue is not None:
            try:
                self._queue.put_nowait(("stop", None))
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                except (EOFError, OSError, ValueError):
                    pass
                try:
                    self._queue.put_nowait(("stop", None))
                except queue.Full:
                    pass
                except (EOFError, OSError, ValueError):
                    pass
            except (EOFError, OSError, ValueError):
                pass

        if self._process is not None:
            self._process.join(timeout=2.0)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1.0)
            self._process = None

        if self._queue is not None:
            try:
                self._queue.close()
                self._queue.join_thread()
            except (EOFError, OSError, ValueError):
                pass
            self._queue = None


class GestureTrackingEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.result_store = ResultStore()
        self.source = OpenCvFrameSource(config.source)
        self._fps = FpsMeter()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_error: str | None = None
        self._running = False
        self._stabilizer = GestureStabilizer(
            stability_frames=config.recognizer.stability_frames,
            missing_reset_frames=config.recognizer.missing_reset_frames,
        )
        self._visualizer = OpenCvVisualizer(
            enabled=config.visualizer.enabled,
            window_name=config.visualizer.window_name,
        )
        self.result_store.publish(self._build_empty_result(status="starting"))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._visualizer.start()
        self._thread = Thread(target=self._run_loop, name="gesture-tracking-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._visualizer.stop()
        self.source.close()
        self._running = False
        self.result_store.publish(self._build_empty_result(status="stopped", error=self._last_error))

    def get_latest_result(self) -> dict[str, Any]:
        payload, _ = self.result_store.get_latest()
        return payload or self._build_empty_result(status="empty")

    def get_status(self) -> dict[str, Any]:
        latest = self.get_latest_result()
        return {
            "running": self._running,
            "last_error": self._last_error,
            "fps": latest.get("fps", 0.0),
            "latest_status": latest.get("status", "unknown"),
            "latest_gesture": latest.get("stable_gesture_name"),
            "video_source": self.source.describe(),
        }

    def _ensure_model(self) -> Path:
        target = Path(self.config.recognizer.model_path).expanduser().resolve()
        if target.exists():
            return target
        if not self.config.recognizer.allow_auto_download:
            raise FileNotFoundError(f"Gesture recognizer model not found: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Downloading MediaPipe gesture recognizer model to %s", target)
        with urllib.request.urlopen(self.config.recognizer.model_url, timeout=60) as response:
            target.write_bytes(response.read())
        return target

    def _run_loop(self) -> None:
        try:
            model_path = self._ensure_model()
            self.source.start()
            self._running = True
            self._last_error = None
        except Exception as exc:
            self._running = False
            self._last_error = str(exc)
            LOGGER.exception("Failed to start gesture tracking")
            self.result_store.publish(self._build_empty_result(status="source_error", error=str(exc)))
            self._visualizer.stop()
            return

        options = vision.GestureRecognizerOptions(
            base_options=python.BaseOptions(
                model_asset_path=str(model_path),
                delegate=python.BaseOptions.Delegate.CPU,
            ),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=int(self.config.recognizer.num_hands),
            min_hand_detection_confidence=float(self.config.recognizer.min_hand_detection_confidence),
            min_hand_presence_confidence=float(self.config.recognizer.min_hand_presence_confidence),
            min_tracking_confidence=float(self.config.recognizer.min_tracking_confidence),
        )

        try:
            with vision.GestureRecognizer.create_from_options(options) as recognizer:
                while not self._stop_event.is_set():
                    timestamp = time.time()
                    try:
                        packet = self.source.read(timestamp)
                        result = self._process_frame(recognizer, packet)
                        self.result_store.publish(result)
                    except Exception as exc:
                        self._last_error = str(exc)
                        LOGGER.exception("Gesture tracking frame failed")
                        self.result_store.publish(self._build_empty_result(status="processing_error", error=str(exc)))
                        time.sleep(0.15)
        finally:
            self._running = False
            self.source.close()
            self._visualizer.stop()

    def _process_frame(self, recognizer: vision.GestureRecognizer, packet: Any) -> dict[str, Any]:
        frame = packet.frame
        frame_height, frame_width = frame.shape[:2]
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb_frame = np.ascontiguousarray(rgb_frame, dtype=np.uint8)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        timestamp_ms = int(packet.timestamp * 1000.0)
        result = recognizer.recognize_for_video(mp_image, timestamp_ms)
        fps = self._fps.tick(packet.timestamp)
        payload = self._build_result_from_recognition(result, frame_width, frame_height, fps, packet)
        if self.config.visualizer.enabled:
            # macOS HighGUI windows are unstable from this worker thread, so
            # we render here and display from a dedicated visualizer process.
            self._visualizer.publish(self._render_overlay(frame.copy(), payload))
        return payload

    def _build_result_from_recognition(
        self,
        result: vision.GestureRecognizerResult,
        frame_width: int,
        frame_height: int,
        fps: float,
        packet: Any,
    ) -> dict[str, Any]:
        gestures = result.gestures or []
        landmarks_batches = result.hand_landmarks or []
        handedness_batches = result.handedness or []

        raw_gesture_name: str | None = None
        raw_gesture_score = 0.0
        gesture_candidates: list[dict[str, Any]] = []
        center_payload: list[float] | None = None
        bbox_payload: list[float] | None = None
        handedness_name: str | None = None
        landmark_payload: list[list[float]] = []
        offset_payload = {"dx": 0.0, "dy": 0.0, "ndx": 0.0, "ndy": 0.0}

        if gestures:
            first_hand = gestures[0] or []
            for category in first_hand[:5]:
                gesture_candidates.append(
                    {
                        "name": str(category.category_name),
                        "score": round(float(category.score), 4),
                    }
                )
            if first_hand:
                top = first_hand[0]
                if float(top.score) >= float(self.config.recognizer.score_threshold):
                    raw_gesture_name = str(top.category_name)
                    raw_gesture_score = float(top.score)

        if landmarks_batches:
            landmarks = landmarks_batches[0]
            xs = [float(point.x) for point in landmarks]
            ys = [float(point.y) for point in landmarks]
            landmark_payload = [
                [
                    round(float(point.x) * frame_width, 3),
                    round(float(point.y) * frame_height, 3),
                ]
                for point in landmarks
            ]
            min_x = max(0.0, min(xs)) * frame_width
            max_x = min(1.0, max(xs)) * frame_width
            min_y = max(0.0, min(ys)) * frame_height
            max_y = min(1.0, max(ys)) * frame_height
            center_x = (min_x + max_x) / 2.0
            center_y = (min_y + max_y) / 2.0
            center_payload = [round(center_x, 3), round(center_y, 3)]
            bbox_payload = [round(min_x, 3), round(min_y, 3), round(max_x, 3), round(max_y, 3)]
            offset_payload = {
                "dx": round(center_x - (frame_width / 2.0), 3),
                "dy": round(center_y - (frame_height / 2.0), 3),
                "ndx": round((center_x - (frame_width / 2.0)) / max(frame_width / 2.0, 1.0), 4),
                "ndy": round((center_y - (frame_height / 2.0)) / max(frame_height / 2.0, 1.0), 4),
            }

        if handedness_batches and handedness_batches[0]:
            handedness_name = str(handedness_batches[0][0].category_name)

        stable_gesture_name, stable_gesture_frames = self._stabilizer.update(raw_gesture_name)
        detected = raw_gesture_name is not None or bool(landmarks_batches)
        status = "tracking" if detected else "no_hand"

        return {
            "timestamp": round(float(packet.timestamp), 6),
            "frame_id": int(packet.frame_id),
            "status": status,
            "detected": bool(detected),
            "gesture_name": raw_gesture_name,
            "gesture_score": round(raw_gesture_score, 4),
            "stable_gesture_name": stable_gesture_name,
            "stable_gesture_frames": int(stable_gesture_frames),
            "handedness": handedness_name,
            "gesture_candidates": gesture_candidates,
            "landmarks": landmark_payload,
            "center": center_payload,
            "bbox": bbox_payload,
            "offset": offset_payload,
            "fps": round(float(fps), 3),
            "frame_size": [int(frame_width), int(frame_height)],
            "recognizer_backend": "mediapipe_gesture_recognizer",
            "video_source": self.source.describe(),
            "error": None,
        }

    def _build_empty_result(self, status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "timestamp": round(time.time(), 6),
            "frame_id": 0,
            "status": status,
            "detected": False,
            "gesture_name": None,
            "gesture_score": 0.0,
            "stable_gesture_name": None,
            "stable_gesture_frames": 0,
            "handedness": None,
            "gesture_candidates": [],
            "landmarks": [],
            "center": None,
            "bbox": None,
            "offset": {"dx": 0.0, "dy": 0.0, "ndx": 0.0, "ndy": 0.0},
            "fps": 0.0,
            "frame_size": [0, 0],
            "recognizer_backend": "mediapipe_gesture_recognizer",
            "video_source": self.source.describe(),
            "error": error,
        }

    def _render_overlay(self, frame: np.ndarray, payload: dict[str, Any]) -> np.ndarray:
        gesture_name = payload.get("gesture_name") or "None"
        stable_name = payload.get("stable_gesture_name") or "None"
        cv2.putText(
            frame,
            f"gesture={gesture_name} stable={stable_name}",
            (20, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        center = payload.get("center")
        if isinstance(center, list) and len(center) == 2:
            cv2.circle(frame, (int(center[0]), int(center[1])), 8, (0, 255, 255), -1)
        landmarks = payload.get("landmarks") or []
        if self.config.visualizer.draw_landmarks and isinstance(landmarks, list):
            for point in landmarks:
                if not isinstance(point, list) or len(point) != 2:
                    continue
                cv2.circle(frame, (int(point[0]), int(point[1])), 4, (0, 200, 255), -1)
            for start_idx, end_idx in HAND_CONNECTIONS:
                if start_idx >= len(landmarks) or end_idx >= len(landmarks):
                    continue
                start_point = landmarks[start_idx]
                end_point = landmarks[end_idx]
                cv2.line(
                    frame,
                    (int(start_point[0]), int(start_point[1])),
                    (int(end_point[0]), int(end_point[1])),
                    (80, 180, 255),
                    2,
                    cv2.LINE_AA,
                )
        bbox = payload.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            cv2.rectangle(
                frame,
                (int(bbox[0]), int(bbox[1])),
                (int(bbox[2]), int(bbox[3])),
                (255, 0, 0),
                2,
            )
        return frame
