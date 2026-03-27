from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from face_tracking.config import SourceConfig


CAPTURE_API_MAP = {
    "auto": None,
    "v4l2": getattr(cv2, "CAP_V4L2", None),
    "gstreamer": getattr(cv2, "CAP_GSTREAMER", None),
    "ffmpeg": getattr(cv2, "CAP_FFMPEG", None),
    "images": getattr(cv2, "CAP_IMAGES", None),
    "avfoundation": getattr(cv2, "CAP_AVFOUNDATION", None),
    "dshow": getattr(cv2, "CAP_DSHOW", None),
    "msmf": getattr(cv2, "CAP_MSMF", None),
}

VIDEO_DEVICE_PATTERN = re.compile(r"^/dev/video(?P<index>\d+)$")
AVFOUNDATION_DEVICE_PATTERN = re.compile(r"\[(?P<index>\d+)\]\s+(?P<name>.+)$")
CONTINUITY_CAMERA_HINTS = ("iphone", "ipad", "desk view", "桌上视角", "capture screen")
CONTINUITY_CAMERA_SUFFIXES = ("的相机",)


@dataclass(frozen=True)
class MacOSVideoDevice:
    index: int
    name: str


def _normalize_device_name(name: str) -> str:
    return name.replace("“", "").replace("”", "").strip().casefold()


def _parse_avfoundation_video_devices(output: str) -> list[MacOSVideoDevice]:
    devices: list[MacOSVideoDevice] = []
    in_video_section = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "AVFoundation video devices:" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices:" in line:
            break
        if not in_video_section:
            continue

        match = AVFOUNDATION_DEVICE_PATTERN.search(line)
        if match:
            devices.append(MacOSVideoDevice(index=int(match.group("index")), name=match.group("name").strip()))

    return devices


def _list_macos_video_devices() -> list[MacOSVideoDevice]:
    if sys.platform != "darwin":
        return []

    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []

    return _parse_avfoundation_video_devices(f"{result.stderr}\n{result.stdout}")


def _find_macos_device_by_name(devices: list[MacOSVideoDevice], camera_name: str) -> MacOSVideoDevice | None:
    normalized_query = _normalize_device_name(camera_name)
    exact_matches = [device for device in devices if _normalize_device_name(device.name) == normalized_query]
    if exact_matches:
        return exact_matches[0]

    partial_matches = [device for device in devices if normalized_query in _normalize_device_name(device.name)]
    if partial_matches:
        return partial_matches[0]
    return None


def _is_deprioritized_macos_camera(device_name: str) -> bool:
    normalized_name = _normalize_device_name(device_name)
    if any(token in normalized_name for token in CONTINUITY_CAMERA_HINTS):
        return True
    return any(normalized_name.endswith(suffix) for suffix in CONTINUITY_CAMERA_SUFFIXES)


def _select_preferred_macos_device(
    devices: list[MacOSVideoDevice],
    requested_index: int | None,
) -> MacOSVideoDevice | None:
    if not devices:
        return None

    devices_by_index = sorted(devices, key=lambda device: device.index)
    requested_device = next((device for device in devices_by_index if device.index == requested_index), None)
    if requested_device and not _is_deprioritized_macos_camera(requested_device.name):
        return requested_device

    preferred_devices = [device for device in devices_by_index if not _is_deprioritized_macos_camera(device.name)]
    if preferred_devices:
        return preferred_devices[0]

    return requested_device or devices_by_index[0]


def _normalize_rotation_deg(value: int) -> int:
    rotation = int(value) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation_deg must be one of 0, 90, 180, 270")
    return rotation


def _rotate_frame(frame: np.ndarray, rotation_deg: int) -> np.ndarray:
    rotation = _normalize_rotation_deg(rotation_deg)
    if rotation == 0:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


class VideoSource:
    def __init__(self, config: SourceConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.capture: cv2.VideoCapture | None = None

    def _resolve_target(self) -> Any:
        if self.config.type == "camera":
            return int(self.config.camera_index)
        if self.config.type == "rtsp":
            if not self.config.rtsp_url:
                raise ValueError("RTSP source selected but `rtsp_url` is empty")
            return self.config.rtsp_url
        if self.config.type == "video_file":
            if not self.config.video_path:
                raise ValueError("Video file source selected but `video_path` is empty")
            video_path = Path(self.config.video_path)
            if not video_path.exists():
                raise FileNotFoundError(f"Video file does not exist: {video_path}")
            return str(video_path)
        if self.config.type == "capture":
            if not self.config.capture_uri:
                raise ValueError("Generic capture source selected but `capture_uri` is empty")
            return self.config.capture_uri
        raise ValueError(f"Unsupported source type: {self.config.type}")

    def _platform_native_capture_api(self) -> int | None:
        if sys.platform == "darwin":
            return CAPTURE_API_MAP["avfoundation"]
        if sys.platform.startswith("linux"):
            return CAPTURE_API_MAP["v4l2"]
        if sys.platform == "win32":
            return CAPTURE_API_MAP["msmf"] or CAPTURE_API_MAP["dshow"]
        return None

    def _resolve_macos_requested_index(self, target: Any) -> int | None:
        if isinstance(target, int):
            return target
        if isinstance(target, str):
            device_match = VIDEO_DEVICE_PATTERN.fullmatch(target)
            if device_match:
                return int(device_match.group("index"))
        return None

    def _resolve_macos_preferred_device(self, target: Any) -> MacOSVideoDevice | None:
        if sys.platform != "darwin":
            return None

        devices = _list_macos_video_devices()
        if not devices:
            return None

        if self.config.camera_name:
            device = _find_macos_device_by_name(devices, self.config.camera_name)
            if device is None:
                available_devices = ", ".join(f"[{item.index}] {item.name}" for item in devices)
                raise RuntimeError(
                    f"Requested macOS camera not found: {self.config.camera_name}. Available devices: {available_devices}"
                )
            return device

        requested_index = self._resolve_macos_requested_index(target)
        if requested_index is None:
            return None
        return _select_preferred_macos_device(devices, requested_index)

    def _build_open_candidates(self, target: Any, api_preference: int | None) -> list[tuple[Any, int | None, str]]:
        candidates: list[tuple[Any, int | None, str]] = []

        if sys.platform == "darwin":
            native_api = self._platform_native_capture_api()
            preferred_device = self._resolve_macos_preferred_device(target)
            if preferred_device is not None:
                selection_reason = (
                    f"macOS requested camera '{preferred_device.name}'"
                    if self.config.camera_name
                    else f"macOS preferred camera '{preferred_device.name}'"
                )
                candidates.append((preferred_device.index, native_api, selection_reason))
                candidates.append((preferred_device.index, None, f"{selection_reason} (auto backend)"))

        candidates.append((target, api_preference, "configured source"))

        if sys.platform == "darwin":
            native_api = self._platform_native_capture_api()
            if isinstance(target, str):
                device_match = VIDEO_DEVICE_PATTERN.fullmatch(target)
                if device_match:
                    index = int(device_match.group("index"))
                    candidates.append((index, native_api, "macOS /dev/videoN fallback"))
                    candidates.append((index, None, "macOS auto backend fallback"))
            elif isinstance(target, int) and api_preference == CAPTURE_API_MAP["v4l2"]:
                candidates.append((target, native_api, "macOS AVFoundation fallback"))
                candidates.append((target, None, "macOS auto backend fallback"))

        unique_candidates: list[tuple[Any, int | None, str]] = []
        seen: set[tuple[Any, int | None]] = set()
        for candidate_target, candidate_api, candidate_reason in candidates:
            key = (candidate_target, candidate_api)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append((candidate_target, candidate_api, candidate_reason))
        return unique_candidates

    def _open_capture(self, target: Any, api_preference: int | None) -> cv2.VideoCapture:
        if api_preference is None:
            return cv2.VideoCapture(target)
        return cv2.VideoCapture(target, api_preference)

    def _describe_api(self, api_preference: int | None) -> str:
        if api_preference is None:
            return "auto"
        for name, value in CAPTURE_API_MAP.items():
            if value == api_preference:
                return name
        return str(api_preference)

    def open(self) -> None:
        target = self._resolve_target()
        api_preference = CAPTURE_API_MAP.get(self.config.api_preference.lower())
        capture: cv2.VideoCapture | None = None
        attempts: list[str] = []

        for candidate_target, candidate_api, candidate_reason in self._build_open_candidates(target, api_preference):
            capture = self._open_capture(candidate_target, candidate_api)
            if capture and capture.isOpened():
                if candidate_reason != "configured source":
                    self.logger.info(
                        "Video source fallback succeeded using %s (%s, backend=%s)",
                        candidate_target,
                        candidate_reason,
                        self._describe_api(candidate_api),
                    )
                break
            attempts.append(f"{candidate_target} ({candidate_reason}, backend={self._describe_api(candidate_api)})")
            if capture is not None:
                capture.release()
                capture = None

        if not capture or not capture.isOpened():
            error_message = f"Unable to open video source: {target}. Tried: {', '.join(attempts)}"
            if sys.platform == "darwin":
                error_message += (
                    ". On macOS, allow camera access for the terminal or IDE in "
                    "System Settings > Privacy & Security > Camera."
                )
            raise RuntimeError(error_message)

        if self.config.width:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        if self.config.height:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        if self.config.fps:
            capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, self.config.buffer_size)

        self.capture = capture
        self.logger.info("Video source opened: %s", self.describe())

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.capture is None:
            return False, None

        ok, frame = self.capture.read()
        if ok:
            frame = _rotate_frame(frame, self.config.rotation_deg)
            return True, frame

        if self.config.type == "video_file" and self.config.loop_video:
            self.logger.info("Video file reached EOF, reopening because loop_video=true")
            self.reconnect()
            if self.capture is None:
                return False, None
            ok, frame = self.capture.read()
            if ok and frame is not None:
                frame = _rotate_frame(frame, self.config.rotation_deg)
            return ok, frame
        return False, None

    def reconnect(self) -> None:
        self.release()
        self.open()

    def release(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def is_opened(self) -> bool:
        return bool(self.capture is not None and self.capture.isOpened())

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.config.type,
            "camera_index": self.config.camera_index,
            "camera_name": self.config.camera_name,
            "rtsp_url": self.config.rtsp_url,
            "video_path": self.config.video_path,
            "capture_uri": self.config.capture_uri,
            "width": self.config.width,
            "height": self.config.height,
            "fps": self.config.fps,
            "rotation_deg": self.config.rotation_deg,
        }
