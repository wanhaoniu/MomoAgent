#!/usr/bin/env python3
"""Capture a single photo from a local camera device.

This module also exposes reusable camera helpers for other local scripts.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = SKILL_ROOT / "workspace" / "picture"
DEFAULT_CAMERA_DEVICE = "/dev/video2"
DEFAULT_CAMERA_WIDTH = 1280
DEFAULT_CAMERA_HEIGHT = 720
DEFAULT_CAMERA_PIXFMT = "auto"
DEFAULT_CAMERA_BACKEND = "auto"
__all__ = ["capture_image", "capture_photo", "save_image", "run_snap", "build_parser"]


def _success_payload(data: Any) -> dict[str, Any]:
    return {"ok": True, "result": data, "error": None}


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "result": None,
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }


def _camera_candidates(raw_device: str) -> list[tuple[Any, int, str]]:
    device = str(raw_device or "").strip()
    if not device:
        raise ValueError("--camera-device is required")
    if device.startswith("/dev/video") and device[len("/dev/video") :].isdigit():
        index = int(device[len("/dev/video") :])
        return [
            (index, cv2.CAP_V4L2, f"index:{index}:v4l2"),
            (index, cv2.CAP_ANY, f"index:{index}:any"),
            (device, cv2.CAP_V4L2, f"path:{device}:v4l2"),
            (device, cv2.CAP_ANY, f"path:{device}:any"),
        ]
    if device.isdigit():
        index = int(device)
        return [
            (index, cv2.CAP_V4L2, f"index:{index}:v4l2"),
            (index, cv2.CAP_ANY, f"index:{index}:any"),
        ]
    return [
        (device, cv2.CAP_V4L2, f"path:{device}:v4l2"),
        (device, cv2.CAP_ANY, f"path:{device}:any"),
    ]


def _camera_device_path(raw_device: str) -> str:
    device = str(raw_device or "").strip()
    if not device:
        raise ValueError("--camera-device is required")
    if device.startswith("/dev/"):
        return device
    if device.isdigit():
        return f"/dev/video{int(device)}"
    raise ValueError(f"Unsupported camera device value: {raw_device!r}")


def _capture_with_opencv(
    *,
    raw_device: str,
    width: int,
    height: int,
    warmup_frames: int,
    pixfmt: str,
    timeout_sec: float,
) -> tuple[Image.Image, dict[str, Any]]:
    errors: list[str] = []
    fourcc = None
    if len(str(pixfmt)) == 4 and str(pixfmt).upper() != "AUTO":
        fourcc = cv2.VideoWriter_fourcc(*str(pixfmt).upper())

    for source, backend, label in _camera_candidates(raw_device):
        deadline = time.time() + max(1.0, float(timeout_sec))
        cap = cv2.VideoCapture(source, backend)
        try:
            if not cap.isOpened():
                errors.append(f"{label}: open failed")
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if fourcc is not None:
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)

            required_frames = max(1, int(warmup_frames) + 1)
            frame = None
            captured = 0
            while time.time() < deadline and captured < required_frames:
                ok, current = cap.read()
                if not ok or current is None or getattr(current, "size", 0) == 0:
                    time.sleep(0.05)
                    continue
                frame = current
                captured += 1
                time.sleep(0.03)

            if frame is None:
                errors.append(f"{label}: no frame received before timeout")
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            return image, {
                "backend": "opencv",
                "source": label,
                "device": str(raw_device),
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
                "pixfmt": str(pixfmt).upper(),
            }
        finally:
            cap.release()

    raise RuntimeError("OpenCV capture failed: " + "; ".join(errors[-4:]))


def _decode_yuyv_frame(data: bytes, *, width: int, height: int) -> Image.Image:
    expected_size = int(width) * int(height) * 2
    if len(data) < expected_size:
        raise RuntimeError(f"YUYV frame too short: expected {expected_size} bytes, got {len(data)}")
    yuyv = np.frombuffer(data[:expected_size], dtype=np.uint8).reshape((int(height), int(width), 2))
    color_code = getattr(cv2, "COLOR_YUV2RGB_YUYV", getattr(cv2, "COLOR_YUV2RGB_YUY2"))
    rgb = cv2.cvtColor(yuyv, color_code)
    return Image.fromarray(rgb)


def _capture_with_v4l2ctl_once(
    *,
    device_path: str,
    width: int,
    height: int,
    warmup_frames: int,
    pixfmt: str,
    timeout_sec: float,
) -> tuple[Image.Image, dict[str, Any]]:
    fd, tmp_path = tempfile.mkstemp(suffix=".frame")
    os.close(fd)
    temp_file = Path(tmp_path)
    try:
        fmt = str(pixfmt).upper()
        command = [
            "v4l2-ctl",
            f"--device={device_path}",
            f"--set-fmt-video=width={int(width)},height={int(height)},pixelformat={fmt}",
            "--stream-mmap=3",
            f"--stream-skip={max(0, int(warmup_frames))}",
            "--stream-count=1",
            f"--stream-to={temp_file}",
        ]
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(2.0, float(timeout_sec)),
        )
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        if "Device or resource busy" in stderr:
            raise RuntimeError(f"Camera {device_path} is busy: {stderr}")
        if proc.returncode != 0:
            detail = stderr or stdout or f"exit code {proc.returncode}"
            raise RuntimeError(f"v4l2-ctl capture failed: {detail}")
        if not temp_file.exists() or temp_file.stat().st_size == 0:
            raise RuntimeError("v4l2-ctl returned success but no frame data was written")

        data = temp_file.read_bytes()
        if fmt == "MJPG":
            try:
                with Image.open(io.BytesIO(data)) as img:
                    image = img.convert("RGB")
            except UnidentifiedImageError as exc:
                raise RuntimeError("v4l2-ctl returned MJPG data that PIL could not decode") from exc
        elif fmt == "YUYV":
            image = _decode_yuyv_frame(data, width=width, height=height)
        else:
            raise RuntimeError(f"Unsupported v4l2 pixel format: {fmt}")

        return image, {
            "backend": "v4l2ctl",
            "source": device_path,
            "device": device_path,
            "width": int(image.width),
            "height": int(image.height),
            "pixfmt": fmt,
        }
    finally:
        temp_file.unlink(missing_ok=True)


def _capture_with_v4l2ctl(
    *,
    raw_device: str,
    width: int,
    height: int,
    warmup_frames: int,
    pixfmt: str,
    timeout_sec: float,
) -> tuple[Image.Image, dict[str, Any]]:
    device_path = _camera_device_path(raw_device)
    formats = ["MJPG", "YUYV"] if str(pixfmt).lower() == "auto" else [str(pixfmt).upper()]
    errors: list[str] = []
    for fmt in formats:
        try:
            return _capture_with_v4l2ctl_once(
                device_path=device_path,
                width=width,
                height=height,
                warmup_frames=warmup_frames,
                pixfmt=fmt,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            errors.append(f"{fmt}: {exc}")
            if "busy" in str(exc).lower():
                break
    raise RuntimeError("v4l2-ctl capture failed: " + "; ".join(errors[-4:]))


def capture_image(
    *,
    raw_device: str,
    width: int,
    height: int,
    warmup_frames: int,
    timeout_sec: float,
    pixfmt: str = DEFAULT_CAMERA_PIXFMT,
    backend: str = DEFAULT_CAMERA_BACKEND,
    retries: int = 1,
    retry_delay_sec: float = 0.0,
) -> tuple[Image.Image, dict[str, Any]]:
    """Capture one image and return both the image and capture metadata."""
    errors: list[str] = []
    backends = ["opencv", "v4l2ctl"] if str(backend) == "auto" else [str(backend)]
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        for current_backend in backends:
            try:
                if current_backend == "opencv":
                    opencv_pixfmt = "MJPG" if str(pixfmt).lower() == "auto" else str(pixfmt).upper()
                    return _capture_with_opencv(
                        raw_device=raw_device,
                        width=width,
                        height=height,
                        warmup_frames=warmup_frames,
                        pixfmt=opencv_pixfmt,
                        timeout_sec=timeout_sec,
                    )
                if current_backend == "v4l2ctl":
                    return _capture_with_v4l2ctl(
                        raw_device=raw_device,
                        width=width,
                        height=height,
                        warmup_frames=warmup_frames,
                        pixfmt=pixfmt,
                        timeout_sec=timeout_sec,
                    )
                raise RuntimeError(f"Unsupported camera backend: {current_backend}")
            except Exception as exc:
                errors.append(f"attempt {attempt} {current_backend}: {exc}")
        if attempt < attempts:
            time.sleep(max(0.0, float(retry_delay_sec)))
    raise RuntimeError("Failed to capture image: " + "; ".join(errors[-6:]))


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output_path:
        return Path(args.output_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    filename = f"{args.prefix or 'camera_snap'}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
    return output_dir / filename


def _normalize_output_path(output_path: Path) -> Path:
    if output_path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return output_path
    return output_path.with_suffix(".jpg")


def save_image(image: Image.Image, output_path: Path, *, quality: int = 95) -> Path:
    """Save a PIL image to disk and return the final output path."""
    output_path = _normalize_output_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(output_path, format="JPEG", quality=max(1, min(100, int(quality))))
    elif output_path.suffix.lower() == ".png":
        image.save(output_path, format="PNG")
    else:
        raise RuntimeError(f"Unsupported image suffix: {output_path.suffix}")
    return output_path


def capture_photo(
    *,
    raw_device: str,
    width: int,
    height: int,
    warmup_frames: int,
    timeout_sec: float,
    output_path: Path,
    quality: int = 95,
    pixfmt: str = DEFAULT_CAMERA_PIXFMT,
    backend: str = DEFAULT_CAMERA_BACKEND,
    retries: int = 1,
    retry_delay_sec: float = 0.0,
) -> dict[str, Any]:
    """Capture one photo and save it to disk."""
    image, capture_meta = capture_image(
        raw_device=raw_device,
        width=width,
        height=height,
        warmup_frames=warmup_frames,
        timeout_sec=timeout_sec,
        pixfmt=pixfmt,
        backend=backend,
        retries=retries,
        retry_delay_sec=retry_delay_sec,
    )
    saved_path = save_image(image, output_path, quality=quality)
    return {
        "action": "camera_snap",
        "image_path": str(saved_path),
        "camera_device": str(raw_device),
        "capture": capture_meta,
        "timestamp": time.time(),
    }


def run_snap(args: argparse.Namespace) -> dict[str, Any]:
    output_path = _resolve_output_path(args)
    return capture_photo(
        raw_device=args.camera_device,
        width=args.camera_width,
        height=args.camera_height,
        warmup_frames=args.camera_warmup_frames,
        timeout_sec=args.camera_timeout,
        output_path=output_path,
        quality=args.jpeg_quality,
        pixfmt=args.camera_pixfmt,
        backend=args.camera_backend,
        retries=args.capture_retries,
        retry_delay_sec=args.capture_retry_delay,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a single photo from a local camera device")
    parser.add_argument(
        "--camera-device",
        default=str(Path(str(os.environ.get("FACE_TRACK_CAMERA_DEVICE", DEFAULT_CAMERA_DEVICE)))),
        help="Camera device path or numeric video index",
    )
    parser.add_argument("--camera-width", type=int, default=DEFAULT_CAMERA_WIDTH)
    parser.add_argument("--camera-height", type=int, default=DEFAULT_CAMERA_HEIGHT)
    parser.add_argument("--camera-warmup-frames", type=int, default=8)
    parser.add_argument("--camera-timeout", type=float, default=4.0)
    parser.add_argument(
        "--camera-pixfmt",
        choices=["auto", "MJPG", "YUYV"],
        default=DEFAULT_CAMERA_PIXFMT,
        help="Preferred camera pixel format",
    )
    parser.add_argument(
        "--camera-backend",
        choices=["auto", "opencv", "v4l2ctl"],
        default=DEFAULT_CAMERA_BACKEND,
        help="Capture backend. auto tries OpenCV first, then v4l2-ctl.",
    )
    parser.add_argument("--capture-retries", type=int, default=1, help="Number of capture retries")
    parser.add_argument("--capture-retry-delay", type=float, default=0.0, help="Delay between capture retries")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--output-path", default="")
    parser.add_argument("--prefix", default="camera_snap")
    parser.add_argument("--jpeg-quality", type=int, default=95)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        print(json.dumps(_success_payload(run_snap(args)), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
