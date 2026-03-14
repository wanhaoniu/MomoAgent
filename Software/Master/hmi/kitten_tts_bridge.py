#!/usr/bin/env python3
"""Persistent KittenTTS bridge for the PyQt HMI process.

Run this script inside a Python 3.12 environment with `kittentts` and
`soundfile` installed. It loads the model once, then accepts JSONL requests:

{"id":"...", "text":"hello", "voice":"Jasper", "output_path":"/tmp/out.wav"}
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent KittenTTS bridge")
    parser.add_argument("--model", default="KittenML/kitten-tts-nano-0.8")
    parser.add_argument("--sample-rate", type=int, default=24000)
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    try:
        from kittentts import KittenTTS
        import soundfile as sf
    except Exception as exc:
        _emit({"id": "", "ok": False, "error": f"KittenTTS imports failed: {exc}"})
        return 1

    try:
        model = KittenTTS(args.model)
    except Exception as exc:
        _emit({"id": "", "ok": False, "error": f"KittenTTS model load failed: {exc}"})
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
            voice = str(payload.get("voice", "")).strip()
            output_path = str(payload.get("output_path", "")).strip()
            if not text:
                raise ValueError("Empty text")
            if not output_path:
                raise ValueError("Missing output_path")

            audio = model.generate(text, voice=voice)
            out_path = Path(output_path).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(out_path), audio, int(args.sample_rate))
            _emit({"id": request_id, "ok": True, "output_path": str(out_path)})
        except Exception as exc:
            _emit({"id": request_id, "ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
