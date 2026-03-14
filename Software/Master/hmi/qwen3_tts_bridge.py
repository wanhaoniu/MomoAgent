#!/usr/bin/env python3
"""Persistent Qwen3-TTS bridge for the PyQt HMI process.

Run this script inside a Python environment with `qwen-tts`, `soundfile`, and
their model dependencies installed. It loads the model once, then accepts JSONL
requests:

{"id":"...", "text":"你好", "voice":"Vivian", "language":"Auto", "instruct":"", "output_path":"/tmp/out.wav"}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _resolve_dtype(torch_mod, raw: str):
    value = str(raw or "").strip().lower()
    if value in {"", "auto"}:
        if torch_mod.cuda.is_available():
            return torch_mod.bfloat16
        return torch_mod.float32
    if value in {"float32", "fp32"}:
        return torch_mod.float32
    if value in {"float16", "fp16"}:
        return torch_mod.float16
    if value in {"bfloat16", "bf16"}:
        return torch_mod.bfloat16
    raise ValueError(f"Unsupported dtype: {raw}")


def _resolve_device(torch_mod, raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "auto"}:
        return "cuda:0" if torch_mod.cuda.is_available() else "cpu"
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent Qwen3-TTS bridge")
    parser.add_argument("--model", default="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    parser.add_argument("--voice", default="Vivian")
    parser.add_argument("--language", default="Auto")
    parser.add_argument("--instruct", default="")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="auto")
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    try:
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel
    except Exception as exc:
        _emit({"id": "", "ok": False, "error": f"Qwen3-TTS imports failed: {exc}"})
        return 1

    try:
        resolved_device = _resolve_device(torch, args.device)
        resolved_dtype = _resolve_dtype(torch, args.dtype)
        model = Qwen3TTSModel.from_pretrained(
            args.model,
            device_map=resolved_device,
            dtype=resolved_dtype,
        )
        model_type = str(getattr(model.model, "tts_model_type", "")).strip().lower()
    except Exception as exc:
        _emit({"id": "", "ok": False, "error": f"Qwen3-TTS model load failed: {exc}"})
        return 1

    for line in sys.stdin:
        raw = str(line or "").strip()
        if not raw:
            continue
        request_id = ""
        try:
            payload = json.loads(raw)
            request_id = str(payload.get("id", "")).strip()
            text = str(payload.get("text", "")).strip()
            voice = str(payload.get("voice", "")).strip() or str(args.voice).strip()
            language = str(payload.get("language", "")).strip() or str(args.language).strip() or "Auto"
            instruct = str(payload.get("instruct", "")).strip() or str(args.instruct).strip()
            output_path = str(payload.get("output_path", "")).strip()
            if not text:
                raise ValueError("Empty text")
            if not output_path:
                raise ValueError("Missing output_path")

            if model_type == "custom_voice":
                wavs, sample_rate = model.generate_custom_voice(
                    text=text,
                    speaker=voice,
                    language=language,
                    instruct=instruct or None,
                )
            elif model_type == "voice_design":
                voice_prompt = instruct or voice
                if not voice_prompt:
                    raise ValueError("VoiceDesign model requires a non-empty voice or instruct prompt")
                wavs, sample_rate = model.generate_voice_design(
                    text=text,
                    instruct=voice_prompt,
                    language=language,
                )
            else:
                raise ValueError(
                    f"Unsupported Qwen3-TTS model type for this bridge: {model_type or 'unknown'}"
                )

            if not wavs:
                raise RuntimeError("Qwen3-TTS returned no waveform")

            audio = wavs[0]
            out_path = Path(output_path).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(out_path), audio, int(sample_rate))
            _emit(
                {
                    "id": request_id,
                    "ok": True,
                    "output_path": str(out_path),
                    "sample_rate": int(sample_rate),
                    "model_type": model_type,
                }
            )
        except Exception as exc:
            _emit({"id": request_id, "ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
