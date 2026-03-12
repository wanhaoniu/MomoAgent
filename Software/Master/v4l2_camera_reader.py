"""Reusable V4L2 camera reader for local USB cameras."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

import cv2
import numpy as np


def normalize_rotation_deg(value: int) -> int:
    """Normalize rotation to 0/90/180/270 degrees."""

    normalized = int(value) % 360
    if normalized not in (0, 90, 180, 270):
        raise ValueError("rotation_deg must be one of 0, 90, 180, 270")
    return normalized


def rotate_frame(frame: np.ndarray, rotation_deg: int) -> np.ndarray:
    """Rotate a frame clockwise by the requested degrees."""

    rotation = normalize_rotation_deg(rotation_deg)
    if rotation == 0:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


@dataclass
class V4L2DeviceInfo:
    """Structured output for one `v4l2-ctl --list-devices` block."""

    name: str
    bus_info: str = ""
    video_nodes: List[str] = field(default_factory=list)
    media_nodes: List[str] = field(default_factory=list)

    def pick_video_node(self, preferred_index: int = 0) -> str:
        if not self.video_nodes:
            raise RuntimeError(f"Device '{self.name}' has no /dev/video* nodes")
        idx = min(max(0, int(preferred_index)), len(self.video_nodes) - 1)
        return self.video_nodes[idx]


def parse_v4l2_list_devices_output(text: str) -> List[V4L2DeviceInfo]:
    """Parse `v4l2-ctl --list-devices` output."""

    devices: List[V4L2DeviceInfo] = []
    current: Optional[V4L2DeviceInfo] = None

    def _flush():
        nonlocal current
        if current is not None and (current.video_nodes or current.media_nodes):
            devices.append(current)
        current = None

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            _flush()
            continue

        if not line.startswith((" ", "\t")):
            _flush()
            header = line.rstrip(":").strip()
            bus_info = ""
            name = header
            if header.endswith(")") and " (" in header:
                name, bus_info = header.rsplit(" (", 1)
                bus_info = bus_info.rstrip(")").strip()
                name = name.strip()
            current = V4L2DeviceInfo(name=name, bus_info=bus_info)
            continue

        if current is None:
            continue

        node = line.strip()
        if node.startswith("/dev/video"):
            current.video_nodes.append(node)
        elif node.startswith("/dev/media"):
            current.media_nodes.append(node)

    _flush()
    return devices


def list_v4l2_devices(timeout_sec: float = 3.0) -> List[V4L2DeviceInfo]:
    """Return all V4L2 devices visible to `v4l2-ctl`."""

    try:
        proc = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_sec)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("`v4l2-ctl` is not installed or not in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`v4l2-ctl --list-devices` timed out") from exc

    if proc.returncode != 0:
        message = str(proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(message or "`v4l2-ctl --list-devices` failed")
    return parse_v4l2_list_devices_output(proc.stdout)


def resolve_v4l2_device(
    device: Optional[str] = None,
    name_hint: Optional[str] = None,
    preferred_video_index: int = 0,
    devices: Optional[Sequence[V4L2DeviceInfo]] = None,
) -> str:
    """Resolve a usable `/dev/video*` node."""

    device_path = str(device or "").strip()
    if device_path:
        return device_path

    infos = list(devices) if devices is not None else list_v4l2_devices()
    if not infos:
        raise RuntimeError("No V4L2 devices found")

    hint = str(name_hint or "").strip().lower()
    if hint:
        matches = [info for info in infos if hint in info.name.lower() or hint in info.bus_info.lower()]
        if not matches:
            raise RuntimeError(f"No V4L2 device matched name hint: {name_hint}")
        return matches[0].pick_video_node(preferred_video_index)

    for info in infos:
        if info.video_nodes:
            return info.pick_video_node(preferred_video_index)
    raise RuntimeError("No usable /dev/video* node found")


def resolve_v4l2_device_candidates(
    device: Optional[str] = None,
    name_hint: Optional[str] = None,
    preferred_video_index: int = 0,
    devices: Optional[Sequence[V4L2DeviceInfo]] = None,
) -> List[str]:
    """Resolve candidate `/dev/video*` nodes ordered by preference."""

    direct_device = str(device or "").strip()
    if direct_device:
        return [direct_device]

    infos = list(devices) if devices is not None else list_v4l2_devices()
    if not infos:
        raise RuntimeError("No V4L2 devices found")

    hint = str(name_hint or "").strip().lower()
    matched_infos = infos
    if hint:
        matched_infos = [info for info in infos if hint in info.name.lower() or hint in info.bus_info.lower()]
        if not matched_infos:
            raise RuntimeError(f"No V4L2 device matched name hint: {name_hint}")

    candidates: List[str] = []
    for info in matched_infos:
        nodes = list(info.video_nodes)
        if not nodes:
            continue
        preferred = min(max(0, int(preferred_video_index)), len(nodes) - 1)
        ordered = [nodes[preferred]] + [node for idx, node in enumerate(nodes) if idx != preferred]
        for node in ordered:
            if node not in candidates:
                candidates.append(node)

    if not candidates:
        raise RuntimeError("No usable /dev/video* node found")
    return candidates


class V4L2CameraReader:
    """Background frame reader for local V4L2 cameras."""

    def __init__(
        self,
        device: Optional[str] = None,
        *,
        name_hint: Optional[str] = None,
        preferred_video_index: int = 0,
        width: Optional[int] = 1280,
        height: Optional[int] = 720,
        fps: Optional[int] = 30,
        pixel_format: Optional[str] = None,
        rotation_deg: int = 0,
        backend: int = cv2.CAP_V4L2,
        buffersize: int = 1,
        reconnect_interval_sec: float = 1.0,
        read_timeout_sec: float = 3.0,
    ):
        self.device = str(device or "").strip()
        self.name_hint = str(name_hint or "").strip()
        self.preferred_video_index = max(0, int(preferred_video_index))
        self.width = int(width) if width else 0
        self.height = int(height) if height else 0
        self.target_fps = int(fps) if fps else 0
        self.pixel_format = str(pixel_format or "").strip().upper()
        self.rotation_deg = normalize_rotation_deg(rotation_deg)
        self.backend = int(backend)
        self.buffersize = max(1, int(buffersize))
        self.reconnect_interval_sec = max(0.2, float(reconnect_interval_sec))
        self.read_timeout_sec = max(0.1, float(read_timeout_sec))

        self.frame: Optional[np.ndarray] = None
        self.fps = 0.0
        self.latency = 0.0
        self.last_frame_ts = 0.0
        self.last_error = ""
        self.running = False

        self._cap: Optional[cv2.VideoCapture] = None
        self._device_path = ""
        self._frame_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._fps_count = 0
        self._fps_window_ts = 0.0
        self._last_success_ts = 0.0

    @property
    def device_path(self) -> str:
        return self._device_path

    def __enter__(self) -> "V4L2CameraReader":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def discover(self) -> List[V4L2DeviceInfo]:
        return list_v4l2_devices()

    def open(self) -> str:
        if self._cap is not None and self._cap.isOpened():
            return self._device_path

        candidates = resolve_v4l2_device_candidates(
            device=self.device,
            name_hint=self.name_hint,
            preferred_video_index=self.preferred_video_index,
        )
        last_error = ""

        for candidate in candidates:
            self._device_path = candidate
            cap = cv2.VideoCapture(self._device_path, self.backend)
            if not cap.isOpened():
                cap.release()
                last_error = f"Failed to open camera: {self._device_path}"
                continue

            self._configure_capture(cap)
            self._cap = cap
            self._fps_count = 0
            self._fps_window_ts = time.time()
            self._last_success_ts = 0.0
            self.last_error = ""
            return self._device_path

        raise RuntimeError(last_error or "Failed to open any candidate V4L2 device")

    def start(self):
        if self.running:
            return
        self.open()
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="V4L2CameraReader", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_capture()
        self._frame_event.clear()

    def get_latest(self, copy: bool = True) -> Tuple[Optional[np.ndarray], float, float]:
        with self._lock:
            frame = None if self.frame is None else (self.frame.copy() if copy else self.frame)
            frame_ts = float(self.last_frame_ts)
            fps = float(self.fps)

        latency_ms = 0.0
        if frame is not None and frame_ts > 0.0:
            latency_ms = max(0.0, (time.time() - frame_ts) * 1000.0)
        self.latency = latency_ms
        return frame, latency_ms, fps

    def read(self, timeout_sec: Optional[float] = None, copy: bool = True) -> Optional[np.ndarray]:
        wait_timeout = self.read_timeout_sec if timeout_sec is None else max(0.0, float(timeout_sec))
        if not self._frame_event.wait(wait_timeout):
            return None
        frame, _, _ = self.get_latest(copy=copy)
        return frame

    def iter_frames(self, timeout_sec: Optional[float] = None, copy: bool = True) -> Iterator[np.ndarray]:
        while self.running:
            frame = self.read(timeout_sec=timeout_sec, copy=copy)
            if frame is not None:
                yield frame

    def capture_once(self, timeout_sec: Optional[float] = None, copy: bool = True) -> Optional[np.ndarray]:
        self.start()
        return self.read(timeout_sec=timeout_sec, copy=copy)

    def save_latest(self, out_path: str | Path) -> str:
        frame, _, _ = self.get_latest(copy=True)
        if frame is None:
            raise RuntimeError("No frame available to save")
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), frame):
            raise RuntimeError(f"Failed to save frame to {target}")
        return str(target)

    def _configure_capture(self, cap: cv2.VideoCapture):
        if self.buffersize > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, float(self.buffersize))
        if self.width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        if self.height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        if self.target_fps > 0:
            cap.set(cv2.CAP_PROP_FPS, float(self.target_fps))
        if len(self.pixel_format) == 4:
            fourcc = cv2.VideoWriter_fourcc(*self.pixel_format)
            cap.set(cv2.CAP_PROP_FOURCC, float(fourcc))

    def _release_capture(self):
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _reopen_capture(self):
        self._release_capture()
        time.sleep(self.reconnect_interval_sec)
        if self._stop_event.is_set():
            return
        self.open()

    def _capture_loop(self):
        while not self._stop_event.is_set():
            cap = self._cap
            if cap is None or not cap.isOpened():
                try:
                    self.open()
                except Exception as exc:
                    self.last_error = str(exc)
                    time.sleep(self.reconnect_interval_sec)
                    continue
                cap = self._cap
                if cap is None:
                    time.sleep(self.reconnect_interval_sec)
                    continue

            ok, frame = cap.read()
            now = time.time()
            if not ok or frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
                self.last_error = f"Failed to read frame from {self._device_path or self.device or self.name_hint}"
                if self._last_success_ts > 0.0 and (now - self._last_success_ts) >= self.read_timeout_sec:
                    try:
                        self._reopen_capture()
                    except Exception as exc:
                        self.last_error = str(exc)
                else:
                    time.sleep(0.02)
                continue

            frame = rotate_frame(frame, self.rotation_deg)
            with self._lock:
                self.frame = frame
                self.last_frame_ts = now
            self._last_success_ts = now
            self._frame_event.set()

            self._fps_count += 1
            dt = now - self._fps_window_ts
            if dt >= 1.0:
                self.fps = float(self._fps_count) / dt
                self._fps_count = 0
                self._fps_window_ts = now

        self._release_capture()


def _print_devices(devices: Sequence[V4L2DeviceInfo]):
    for info in devices:
        print(f"{info.name} ({info.bus_info or 'unknown-bus'})")
        for node in info.video_nodes:
            print(f"  {node}")
        for node in info.media_nodes:
            print(f"  {node}")


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or test a local V4L2 camera.")
    parser.add_argument("--list", action="store_true", help="List detected V4L2 devices and exit.")
    parser.add_argument("--device", default="", help="Direct device path, for example /dev/video0.")
    parser.add_argument("--name-hint", default="", help="Pick the first camera whose name contains this value.")
    parser.add_argument("--video-index", type=int, default=0, help="Index inside one camera block when multiple /dev/video* nodes exist.")
    parser.add_argument("--width", type=int, default=1280, help="Requested capture width.")
    parser.add_argument("--height", type=int, default=720, help="Requested capture height.")
    parser.add_argument("--fps", type=int, default=30, help="Requested capture FPS.")
    parser.add_argument("--pixel-format", default="", help="Optional 4-char V4L2 pixel format, for example MJPG.")
    parser.add_argument("--rotate", type=int, default=0, help="Rotate output clockwise. Supported: 0, 90, 180, 270.")
    parser.add_argument("--save", default="", help="Save one frame to this path.")
    parser.add_argument("--show", action="store_true", help="Open an OpenCV preview window.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Frame wait timeout in seconds.")
    return parser


def main() -> int:
    args = _build_argparser().parse_args()

    if args.list:
        _print_devices(list_v4l2_devices())
        return 0

    reader = V4L2CameraReader(
        device=args.device or None,
        name_hint=args.name_hint or None,
        preferred_video_index=args.video_index,
        width=args.width,
        height=args.height,
        fps=args.fps,
        pixel_format=args.pixel_format or None,
        rotation_deg=args.rotate,
        read_timeout_sec=args.timeout,
    )

    try:
        reader.start()
        frame = reader.read(timeout_sec=args.timeout, copy=True)
        if frame is None:
            raise RuntimeError("Timed out waiting for the first frame")

        print(f"device={reader.device_path}")
        print(f"shape={frame.shape}")

        if args.save:
            saved = reader.save_latest(args.save)
            print(f"saved={saved}")

        if args.show:
            while True:
                frame, latency_ms, fps = reader.get_latest(copy=True)
                if frame is None:
                    frame = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(frame, "No Signal", (220, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (128, 128, 128), 2)
                else:
                    cv2.putText(
                        frame,
                        f"FPS: {fps:.1f}  Latency: {latency_ms:.1f} ms",
                        (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 0),
                        2,
                    )
                cv2.imshow("V4L2 Camera", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
            cv2.destroyAllWindows()
        return 0
    finally:
        reader.stop()


if __name__ == "__main__":
    raise SystemExit(main())
