#!/usr/bin/env python3
"""Magic mirror demo helper for camera capture and queen-video generation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = SKILL_ROOT / "workspace" / "runtime"
PHOTOS_DIR = RUNTIME_DIR / "photos"
GENERATED_DIR = RUNTIME_DIR / "generated"
STATE_PATH = RUNTIME_DIR / "magic_mirror_state.json"

DEFAULT_SOARMMOCE_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "soarmmoce-real-con"
DEFAULT_ARTSAPI_SKILL_ROOT = Path.home() / ".openclaw" / "skills" / "artsapi-image-video"
DEFAULT_SOARMMOCE_PYTHON = Path.home() / "miniforge3" / "envs" / "soarmmoce" / "bin" / "python"
DEFAULT_YUNET_MODEL = (
    Path.home()
    / "Documents"
    / "Project"
    / "SO-ARM-MOCE"
    / "Software"
    / "Master"
    / "face_loc"
    / "weights"
    / "face_detection_yunet_2023mar.onnx"
)

DEFAULT_CAMERA_DEVICE = "0"
DEFAULT_CAMERA_WIDTH = 1280
DEFAULT_CAMERA_HEIGHT = 720
DEFAULT_CAMERA_WARMUP_FRAMES = 8
DEFAULT_CAMERA_TIMEOUT = 4.0
DEFAULT_CAPTURE_PREFIX = "magic_mirror"
DEFAULT_CAPTURE_RETRIES = 2
DEFAULT_CAPTURE_RETRY_DELAY = 0.2
DEFAULT_UPLOAD_PROVIDER = "catbox"
DEFAULT_UPLOAD_EXPIRES_HOURS = 168

DEFAULT_QUEEN_PROMPT = (
    "让照片中的人物从真实状态逐步变身为高贵皇后，"
    "镜头缓慢推进，出现精致王冠、华丽礼服、金色光效与童话魔镜氛围，"
    "保持同一个人脸和身份一致，动作自然，适合短视频展示。"
)

cv2 = None
np = None
FACE_DETECTOR = None


def _success_payload(data: Any) -> dict[str, Any]:
    return {"ok": True, "result": data, "error": None}


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "result": None,
        "error": {"type": exc.__class__.__name__, "message": str(exc)},
    }


def _env_path(key: str, default: Path) -> Path:
    raw = str(os.environ.get(key, "") or "").strip()
    return Path(raw).expanduser() if raw else default


def _python_bin() -> str:
    return str(Path(sys.executable or "python3"))


def _preferred_python_bin() -> str:
    raw = str(os.environ.get("MAGIC_MIRROR_PYTHON", "") or "").strip()
    if raw:
        return raw
    if DEFAULT_SOARMMOCE_PYTHON.exists():
        return str(DEFAULT_SOARMMOCE_PYTHON)
    return _python_bin()


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _ensure_cv_stack() -> tuple[Any, Any]:
    global cv2, np
    if cv2 is None or np is None:
        import cv2 as _cv2
        import numpy as _np

        cv2 = _cv2
        np = _np
    return cv2, np


def _maybe_reexec_for_cv_stack(args: argparse.Namespace) -> int | None:
    if args.cmd not in {"pick-best", "queen-video"}:
        return None
    if _module_available("cv2") and _module_available("numpy"):
        return None

    preferred_python = _preferred_python_bin()
    if Path(preferred_python).resolve() == Path(_python_bin()).resolve():
        return None

    proc = subprocess.run([preferred_python, str(Path(__file__).resolve()), *sys.argv[1:]], text=True, check=False)
    return int(proc.returncode)


def _camera_snap_script() -> Path:
    root = _env_path("MAGIC_MIRROR_SOARMMOCE_SKILL_ROOT", DEFAULT_SOARMMOCE_SKILL_ROOT)
    script = root / "scripts" / "soarmmoce_camera_snap.py"
    if not script.exists():
        raise FileNotFoundError(f"camera snap script not found: {script}")
    return script


def _artsapi_cli_script() -> Path:
    root = _env_path("MAGIC_MIRROR_ARTSAPI_SKILL_ROOT", DEFAULT_ARTSAPI_SKILL_ROOT)
    script = root / "scripts" / "artsapi_cli.py"
    if not script.exists():
        raise FileNotFoundError(f"ArtsAPI CLI script not found: {script}")
    return script


def _yunet_model_path() -> Path | None:
    raw = str(os.environ.get("MAGIC_MIRROR_YUNET_MODEL", "") or "").strip()
    candidate = Path(raw).expanduser() if raw else DEFAULT_YUNET_MODEL
    return candidate if candidate.exists() else None


def _ensure_runtime_dirs() -> None:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON output, got: {stdout.strip() or '<empty>'}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected JSON object output")
    return payload


def _is_public_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(str(value).strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _upload_image_to_public_url(path: Path, *, provider: str, expires_hours: int | None = None) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"Upload source image not found: {path}")

    normalized_provider = str(provider or "").strip().lower() or DEFAULT_UPLOAD_PROVIDER
    if normalized_provider != "catbox":
        raise RuntimeError(f"Unsupported upload provider: {provider}")

    command = [
        "curl",
        "-fsS",
        "-A",
        "openclaw-magic-mirror-demo/1.0",
        "-X",
        "POST",
        "https://catbox.moe/user/api.php",
        "-F",
        "reqtype=fileupload",
        "-F",
        f"fileToUpload=@{path}",
    ]
    proc = _run_command(command)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"upload failed: {' '.join(command)}"
        raise RuntimeError(message)

    public_url = proc.stdout.strip()
    if not _is_public_http_url(public_url):
        raise RuntimeError(f"Upload provider did not return a valid public URL: {public_url or '<empty>'}")

    result = {
        "provider": normalized_provider,
        "source_path": str(path),
        "public_url": public_url,
    }
    if expires_hours:
        result["expires_hours"] = int(expires_hours)
    return result


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(payload: dict[str, Any]) -> None:
    _ensure_runtime_dirs()
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_image(path: Path) -> np.ndarray:
    _cv2, _np = _ensure_cv_stack()
    data = _np.fromfile(str(path), dtype=_np.uint8)
    image = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise RuntimeError(f"Failed to decode image: {path}")
    return image


class FaceCandidate(dict):
    pass


class FaceDetector:
    def __init__(self) -> None:
        _cv2, _ = _ensure_cv_stack()
        self._yunet = None
        self._haar = None

        model_path = _yunet_model_path()
        if model_path is not None and hasattr(_cv2, "FaceDetectorYN_create"):
            try:
                self._yunet = _cv2.FaceDetectorYN_create(str(model_path), "", (320, 320))
            except Exception:
                self._yunet = None

        cascade_path = Path(_cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if cascade_path.exists():
            self._haar = _cv2.CascadeClassifier(str(cascade_path))
            if self._haar.empty():
                self._haar = None

    def detect(self, image: np.ndarray) -> list[FaceCandidate]:
        _cv2, _ = _ensure_cv_stack()
        height, width = image.shape[:2]
        if self._yunet is not None:
            try:
                self._yunet.setInputSize((int(width), int(height)))
                _, detections = self._yunet.detect(image)
                if detections is not None and len(detections) > 0:
                    results: list[FaceCandidate] = []
                    for row in detections:
                        x, y, w, h = [float(v) for v in row[:4]]
                        confidence = float(row[-1])
                        center = (x + (w / 2.0), y + (h / 2.0))
                        results.append(
                            FaceCandidate(
                                x=x,
                                y=y,
                                w=w,
                                h=h,
                                confidence=confidence,
                                center_x=center[0],
                                center_y=center[1],
                                area=float(max(0.0, w * h)),
                                detector="yunet",
                            )
                        )
                    return results
            except Exception:
                pass

        if self._haar is not None:
            gray = _cv2.cvtColor(image, _cv2.COLOR_BGR2GRAY)
            faces = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            results = []
            for (x, y, w, h) in faces:
                center = (float(x) + (float(w) / 2.0), float(y) + (float(h) / 2.0))
                results.append(
                    FaceCandidate(
                        x=float(x),
                        y=float(y),
                        w=float(w),
                        h=float(h),
                        confidence=0.5,
                        center_x=center[0],
                        center_y=center[1],
                        area=float(w * h),
                        detector="haar",
                    )
                )
            return results

        return []


def _face_detector() -> FaceDetector:
    global FACE_DETECTOR
    if FACE_DETECTOR is None:
        FACE_DETECTOR = FaceDetector()
    return FACE_DETECTOR


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _normalized_center_score(center_x: float, center_y: float, width: int, height: int) -> float:
    frame_center_x = width / 2.0
    frame_center_y = height / 2.0
    distance = math.hypot(center_x - frame_center_x, center_y - frame_center_y)
    max_distance = math.hypot(frame_center_x, frame_center_y)
    if max_distance <= 0:
        return 0.0
    return 1.0 - _clamp(distance / max_distance, 0.0, 1.0)


def _brightness_score(gray: np.ndarray) -> float:
    brightness = float(gray.mean()) / 255.0
    return 1.0 - _clamp(abs(brightness - 0.55) / 0.55, 0.0, 1.0)


def _sharpness_score(gray: np.ndarray) -> tuple[float, float]:
    _cv2, _ = _ensure_cv_stack()
    sharpness = float(_cv2.Laplacian(gray, _cv2.CV_64F).var())
    return sharpness, _clamp(sharpness / 450.0, 0.0, 1.0)


def _score_photo(path: Path) -> dict[str, Any]:
    _cv2, _ = _ensure_cv_stack()
    image = _read_image(path)
    height, width = image.shape[:2]
    gray = _cv2.cvtColor(image, _cv2.COLOR_BGR2GRAY)

    faces = _face_detector().detect(image)
    primary_face = max(faces, key=lambda item: float(item["area"]), default=None)

    sharpness_raw, sharpness_score = _sharpness_score(gray)
    brightness_score = _brightness_score(gray)
    metrics: dict[str, Any] = {
        "path": str(path),
        "width": int(width),
        "height": int(height),
        "sharpness_raw": sharpness_raw,
        "sharpness_score": sharpness_score,
        "brightness_score": brightness_score,
        "faces_detected": len(faces),
    }

    if primary_face is not None:
        face_area_ratio = float(primary_face["area"]) / float(max(1, width * height))
        center_score = _normalized_center_score(
            float(primary_face["center_x"]),
            float(primary_face["center_y"]),
            width,
            height,
        )
        face_size_score = _clamp(face_area_ratio / 0.18, 0.0, 1.0)
        confidence_score = _clamp(float(primary_face["confidence"]), 0.0, 1.0)
        total_score = (
            (face_size_score * 0.40)
            + (center_score * 0.20)
            + (confidence_score * 0.20)
            + (sharpness_score * 0.15)
            + (brightness_score * 0.05)
        )
        metrics.update(
            {
                "selection_reason": "largest_face_portrait_score",
                "primary_face": dict(primary_face),
                "face_area_ratio": face_area_ratio,
                "face_size_score": face_size_score,
                "center_score": center_score,
                "confidence_score": confidence_score,
                "total_score": total_score,
            }
        )
        return metrics

    total_score = (sharpness_score * 0.7) + (brightness_score * 0.3)
    metrics.update(
        {
            "selection_reason": "no_face_fallback_quality_score",
            "primary_face": None,
            "face_area_ratio": 0.0,
            "face_size_score": 0.0,
            "center_score": 0.0,
            "confidence_score": 0.0,
            "total_score": total_score,
        }
    )
    return metrics


def _photo_candidates(photos_dir: Path, max_photos: int) -> list[Path]:
    all_paths = [
        path
        for path in photos_dir.glob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    all_paths.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    real_paths = [path for path in all_paths if not path.name.lower().startswith("mock_")]
    preferred = real_paths if real_paths else all_paths
    return preferred[: max(1, int(max_photos))]


def _capture_with_camera(args: argparse.Namespace) -> dict[str, Any]:
    _ensure_runtime_dirs()
    command = [
        _python_bin(),
        str(_camera_snap_script()),
        "--camera-device",
        str(args.camera_device),
        "--camera-width",
        str(int(args.camera_width)),
        "--camera-height",
        str(int(args.camera_height)),
        "--camera-warmup-frames",
        str(int(args.camera_warmup_frames)),
        "--camera-timeout",
        str(float(args.camera_timeout)),
        "--camera-backend",
        str(args.camera_backend),
        "--capture-retries",
        str(int(args.capture_retries)),
        "--capture-retry-delay",
        str(float(args.capture_retry_delay)),
        "--output-dir",
        str(PHOTOS_DIR),
        "--prefix",
        str(args.prefix),
    ]
    proc = _run_command(command)
    payload = _parse_json_stdout(proc.stdout)
    if proc.returncode != 0 or not bool(payload.get("ok")):
        error = payload.get("error") or {}
        message = error.get("message") or proc.stderr.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(str(message))

    result = payload.get("result") or {}
    image_path = str(result.get("image_path") or "").strip()
    if not image_path:
        raise RuntimeError("Camera snap command succeeded but did not return image_path")

    state = _load_state()
    state["last_capture_path"] = image_path
    state["last_action"] = "capture"
    state["last_capture_time"] = time.time()
    _save_state(state)

    return {
        "action": "capture",
        "image_path": image_path,
        "photos_dir": str(PHOTOS_DIR),
        "camera": result,
    }


def _pick_best_photo(args: argparse.Namespace) -> dict[str, Any]:
    _ensure_runtime_dirs()
    candidates = _photo_candidates(PHOTOS_DIR, args.max_photos)
    if not candidates:
        raise RuntimeError(f"No photos found in {PHOTOS_DIR}")

    scored: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in candidates:
        try:
            scored.append(_score_photo(path))
        except Exception as exc:
            failures.append({"path": str(path), "error": str(exc)})

    if not scored:
        raise RuntimeError("Failed to analyze all saved photos")

    scored.sort(key=lambda item: float(item.get("total_score") or 0.0), reverse=True)
    best = scored[0]

    state = _load_state()
    state["last_best_photo_path"] = best["path"]
    state["last_action"] = "pick_best"
    state["last_best_photo_time"] = time.time()
    _save_state(state)

    return {
        "action": "pick_best",
        "photos_dir": str(PHOTOS_DIR),
        "best_photo_path": best["path"],
        "best_photo_score": best["total_score"],
        "best_photo_metrics": best,
        "candidates_considered": scored,
        "analysis_failures": failures,
        "note": "Current 'most beautiful' choice is a portrait-quality heuristic based on face prominence, center, sharpness, brightness, and face confidence.",
    }


def _extract_saved_files(response: dict[str, Any]) -> list[str]:
    artifacts = response.get("_local_artifacts")
    if not isinstance(artifacts, dict):
        return []
    saved_files = artifacts.get("saved_files")
    if not isinstance(saved_files, list):
        return []
    return [str(item).strip() for item in saved_files if str(item).strip()]


def _run_artsapi_video(
    *,
    image_path: str,
    prompt: str,
    model: str | None,
    duration: int | None,
    resolution: str | None,
    ratio: str | None,
    negative_prompt: str | None,
    timeout: float,
    poll_interval: int,
    max_wait: int,
) -> dict[str, Any]:
    _ensure_runtime_dirs()
    command = [
        _python_bin(),
        str(_artsapi_cli_script()),
        "--timeout",
        str(int(timeout)),
        "video",
        "--prompt",
        prompt,
        "--image-url",
        image_path,
        "--save-local",
        "--save-dir",
        str(GENERATED_DIR),
        "--poll-interval",
        str(int(poll_interval)),
        "--max-wait",
        str(int(max_wait)),
    ]
    if model:
        command.extend(["--model", model])
    if duration is not None:
        command.extend(["--duration", str(int(duration))])
    if resolution:
        command.extend(["--resolution", resolution])
    if ratio:
        command.extend(["--ratio", ratio])
    if negative_prompt:
        command.extend(["--negative-prompt", negative_prompt])

    proc = _run_command(command)
    payload = _parse_json_stdout(proc.stdout)
    if proc.returncode != 0:
        message = payload.get("msg") or proc.stderr.strip() or f"command failed: {' '.join(command)}"
        raise RuntimeError(str(message))
    return {
        "command": command,
        "stdout": payload,
    }


def _queen_video(args: argparse.Namespace) -> dict[str, Any]:
    if args.image_path:
        raw_image_path = str(args.image_path).strip()
        if _is_public_http_url(raw_image_path):
            source_photo_path = raw_image_path
        else:
            source_photo_path = str(Path(raw_image_path).expanduser().resolve())
            if not Path(source_photo_path).exists():
                raise RuntimeError(f"Specified image does not exist: {source_photo_path}")
        best_photo = None
    else:
        best_photo = _pick_best_photo(argparse.Namespace(max_photos=args.max_photos))
        source_photo_path = str(best_photo["best_photo_path"])

    prompt = str(args.prompt or os.environ.get("MAGIC_MIRROR_QUEEN_PROMPT") or DEFAULT_QUEEN_PROMPT).strip()
    upload_result = None
    artsapi_image_ref = source_photo_path
    if not _is_public_http_url(source_photo_path):
        upload_result = _upload_image_to_public_url(
            Path(source_photo_path),
            provider=args.upload_provider,
            expires_hours=args.upload_expires_hours,
        )
        artsapi_image_ref = str(upload_result["public_url"])

    artsapi_result = _run_artsapi_video(
        image_path=artsapi_image_ref,
        prompt=prompt,
        model=args.model,
        duration=args.duration,
        resolution=args.resolution,
        ratio=args.ratio,
        negative_prompt=args.negative_prompt,
        timeout=float(args.timeout),
        poll_interval=int(args.poll_interval),
        max_wait=int(args.max_wait),
    )
    saved_files = _extract_saved_files(artsapi_result["stdout"])
    queen_video_path = saved_files[0] if saved_files else None

    state = _load_state()
    state["last_best_photo_path"] = source_photo_path
    state["last_uploaded_source_url"] = artsapi_image_ref if artsapi_image_ref != source_photo_path else None
    state["last_queen_video_path"] = queen_video_path
    state["last_action"] = "queen_video"
    state["last_queen_prompt"] = prompt
    state["last_queen_time"] = time.time()
    _save_state(state)

    return {
        "action": "queen_video",
        "source_photo_path": source_photo_path,
        "artsapi_image_ref": artsapi_image_ref,
        "upload": upload_result,
        "best_photo": best_photo,
        "prompt": prompt,
        "queen_video_local_path": queen_video_path,
        "generated_dir": str(GENERATED_DIR),
        "artsapi": artsapi_result["stdout"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Magic mirror demo helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    capture = sub.add_parser("capture", help="Capture one photo from the local SOARM camera into the magic mirror workspace")
    capture.add_argument("--camera-device", default=os.environ.get("MAGIC_MIRROR_CAMERA_DEVICE", DEFAULT_CAMERA_DEVICE))
    capture.add_argument("--camera-width", type=int, default=DEFAULT_CAMERA_WIDTH)
    capture.add_argument("--camera-height", type=int, default=DEFAULT_CAMERA_HEIGHT)
    capture.add_argument("--camera-warmup-frames", type=int, default=DEFAULT_CAMERA_WARMUP_FRAMES)
    capture.add_argument("--camera-timeout", type=float, default=DEFAULT_CAMERA_TIMEOUT)
    capture.add_argument("--camera-backend", choices=["auto", "opencv", "v4l2ctl"], default="opencv")
    capture.add_argument("--capture-retries", type=int, default=DEFAULT_CAPTURE_RETRIES)
    capture.add_argument("--capture-retry-delay", type=float, default=DEFAULT_CAPTURE_RETRY_DELAY)
    capture.add_argument("--prefix", default=DEFAULT_CAPTURE_PREFIX)

    pick_best = sub.add_parser("pick-best", help="Choose the best portrait candidate from saved magic mirror photos")
    pick_best.add_argument("--max-photos", type=int, default=50)

    queen_video = sub.add_parser("queen-video", help="Pick the best saved photo and turn it into a queen video via ArtsAPI")
    queen_video.add_argument("--image-path", default=None, help="Optional explicit source photo path. Otherwise use the best saved photo.")
    queen_video.add_argument("--max-photos", type=int, default=50)
    queen_video.add_argument("--prompt", default=None)
    queen_video.add_argument("--model", default=None)
    queen_video.add_argument("--duration", type=int, default=5)
    queen_video.add_argument("--resolution", default="720p")
    queen_video.add_argument("--ratio", default="16:9")
    queen_video.add_argument("--negative-prompt", default=None)
    queen_video.add_argument("--upload-provider", default=os.environ.get("MAGIC_MIRROR_UPLOAD_PROVIDER", DEFAULT_UPLOAD_PROVIDER))
    queen_video.add_argument("--upload-expires-hours", type=int, default=DEFAULT_UPLOAD_EXPIRES_HOURS)
    queen_video.add_argument("--timeout", type=float, default=120.0)
    queen_video.add_argument("--poll-interval", type=int, default=5)
    queen_video.add_argument("--max-wait", type=int, default=900)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    reexec_code = _maybe_reexec_for_cv_stack(args)
    if reexec_code is not None:
        raise SystemExit(reexec_code)
    try:
        if args.cmd == "capture":
            result = _capture_with_camera(args)
            if "image_path" in result and result["image_path"]:
                try:
                    __import__("subprocess").run(["open", result["image_path"]], check=False)
                except Exception: pass
        elif args.cmd == "pick-best":
            result = _pick_best_photo(args)
            if "best_photo_path" in result and result["best_photo_path"]:
                try:
                    __import__("subprocess").run(["open", result["best_photo_path"]], check=False)
                except Exception: pass
        elif args.cmd == "queen-video":
            result = _queen_video(args)
        else:
            raise ValueError(f"Unsupported command: {args.cmd}")
        print(json.dumps(_success_payload(result), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
