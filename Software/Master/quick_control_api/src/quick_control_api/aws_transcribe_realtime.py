from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

try:
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.model import TranscriptEvent

    AMAZON_TRANSCRIBE_AVAILABLE = True
    AMAZON_TRANSCRIBE_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised through runtime fallback
    TranscribeStreamingClient = None
    TranscriptEvent = None
    AMAZON_TRANSCRIBE_AVAILABLE = False
    AMAZON_TRANSCRIBE_IMPORT_ERROR = exc


DEFAULT_REGION = str(
    os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
).strip() or "us-east-2"
DEFAULT_LANGUAGE_CODE = "zh-CN"
DEFAULT_MEDIA_ENCODING = "pcm"
DEFAULT_SAMPLE_RATE_HZ = 16000
DEFAULT_PARTIAL_RESULTS_STABILITY = "medium"
MAX_AUDIO_CHUNK_BYTES = 32 * 1024
VALID_MEDIA_ENCODINGS = {"pcm", "flac", "ogg-opus"}
VALID_PARTIAL_STABILITIES = {"low", "medium", "high"}


@dataclass(frozen=True, slots=True)
class AwsRealtimeSttConfig:
    region: str = DEFAULT_REGION
    language_code: str = DEFAULT_LANGUAGE_CODE
    media_encoding: str = DEFAULT_MEDIA_ENCODING
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    partial_results_stability: str = DEFAULT_PARTIAL_RESULTS_STABILITY
    max_audio_chunk_bytes: int = MAX_AUDIO_CHUNK_BYTES


def load_aws_realtime_stt_config() -> AwsRealtimeSttConfig:
    region = str(os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION).strip()
    language_code = str(os.getenv("AWS_TRANSCRIBE_LANGUAGE_CODE", DEFAULT_LANGUAGE_CODE) or "").strip()
    media_encoding = str(os.getenv("AWS_TRANSCRIBE_MEDIA_ENCODING", DEFAULT_MEDIA_ENCODING) or "").strip().lower()
    sample_rate_hz = _read_int(
        os.getenv("AWS_TRANSCRIBE_SAMPLE_RATE_HZ"),
        default=DEFAULT_SAMPLE_RATE_HZ,
        minimum=8000,
    )
    stability = str(
        os.getenv("AWS_TRANSCRIBE_PARTIAL_RESULTS_STABILITY", DEFAULT_PARTIAL_RESULTS_STABILITY)
        or ""
    ).strip().lower()
    if media_encoding not in VALID_MEDIA_ENCODINGS:
        media_encoding = DEFAULT_MEDIA_ENCODING
    if stability not in VALID_PARTIAL_STABILITIES:
        stability = DEFAULT_PARTIAL_RESULTS_STABILITY
    return AwsRealtimeSttConfig(
        region=region or DEFAULT_REGION,
        language_code=language_code or DEFAULT_LANGUAGE_CODE,
        media_encoding=media_encoding or DEFAULT_MEDIA_ENCODING,
        sample_rate_hz=sample_rate_hz,
        partial_results_stability=stability,
        max_audio_chunk_bytes=MAX_AUDIO_CHUNK_BYTES,
    )


def status_payload(config: Optional[AwsRealtimeSttConfig] = None) -> dict[str, Any]:
    resolved = config or load_aws_realtime_stt_config()
    return {
        "available": bool(AMAZON_TRANSCRIBE_AVAILABLE),
        "region": resolved.region,
        "language_code": resolved.language_code,
        "media_encoding": resolved.media_encoding,
        "sample_rate_hz": resolved.sample_rate_hz,
        "partial_results_stability": resolved.partial_results_stability,
        "max_audio_chunk_bytes": resolved.max_audio_chunk_bytes,
        "import_error": str(AMAZON_TRANSCRIBE_IMPORT_ERROR or "").strip(),
    }


class AwsRealtimeSttSession:
    def __init__(self, *, config: Optional[AwsRealtimeSttConfig] = None) -> None:
        self._config = config or load_aws_realtime_stt_config()
        self._send_lock = asyncio.Lock()
        self._client = None
        self._stream = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._state = "idle"
        self._session_id = ""
        self._final_segments = 0
        self._partial_segments = 0
        self._audio_bytes = 0
        self._last_error = ""
        self._stream_stop_sent = False

    @property
    def config(self) -> AwsRealtimeSttConfig:
        return self._config

    @property
    def state(self) -> str:
        return self._state

    async def send_json(self, websocket, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await websocket.send_json(payload)

    async def send_error(
        self,
        websocket,
        *,
        stage: str,
        message: str,
        code: str = "ERROR",
    ) -> None:
        await self.send_json(
            websocket,
            {
                "type": "error",
                "stage": str(stage or "").strip() or "unknown",
                "code": str(code or "").strip() or "ERROR",
                "message": str(message or "").strip() or "Unknown error",
            },
        )

    async def send_ready(self, websocket) -> None:
        await self.send_json(
            websocket,
            {
                "type": "ready",
                "data": {
                    "state": self._state,
                    "config": status_payload(self._config),
                    "expected_audio": {
                        "mediaEncoding": self._config.media_encoding,
                        "sampleRateHertz": self._config.sample_rate_hz,
                        "channels": 1,
                        "chunkDurationMs": 100,
                    },
                },
            },
        )

    async def start(self, websocket, payload: dict[str, Any]) -> None:
        if not AMAZON_TRANSCRIBE_AVAILABLE or TranscribeStreamingClient is None:
            raise RuntimeError(
                "amazon-transcribe is unavailable: "
                f"{str(AMAZON_TRANSCRIBE_IMPORT_ERROR or 'unknown import error').strip()}"
            )
        if self._stream is not None or self._reader_task is not None:
            raise RuntimeError("STT stream is already running on this WebSocket connection")

        config = self._resolve_request_config(payload)
        self._config = config
        self._client = TranscribeStreamingClient(region=config.region)
        raw_session_id = str(payload.get("sessionId", "") or "").strip()
        self._session_id = raw_session_id or str(uuid.uuid4())
        self._state = "starting"
        self._final_segments = 0
        self._partial_segments = 0
        self._audio_bytes = 0
        self._last_error = ""
        self._stream_stop_sent = False

        stream = await self._client.start_stream_transcription(
            language_code=config.language_code,
            media_sample_rate_hz=int(config.sample_rate_hz),
            media_encoding=config.media_encoding,
            session_id=self._session_id,
            enable_partial_results_stabilization=True,
            partial_results_stability=config.partial_results_stability,
        )
        self._stream = stream
        self._state = "streaming"
        self._reader_task = asyncio.create_task(self._forward_transcripts(websocket, stream))
        await self.send_json(
            websocket,
            {
                "type": "stream_started",
                "data": {
                    "sessionId": self._session_id,
                    "region": config.region,
                    "languageCode": config.language_code,
                    "mediaEncoding": config.media_encoding,
                    "sampleRateHertz": config.sample_rate_hz,
                    "partialResultsStability": config.partial_results_stability,
                },
            },
        )

    async def send_audio(self, audio_chunk: bytes) -> None:
        if self._stream is None:
            raise RuntimeError("STT stream is not started yet")
        if not audio_chunk:
            return

        view = memoryview(audio_chunk)
        offset = 0
        max_size = int(self._config.max_audio_chunk_bytes)
        while offset < len(view):
            next_offset = min(offset + max_size, len(view))
            chunk = bytes(view[offset:next_offset])
            await self._stream.input_stream.send_audio_event(audio_chunk=chunk)
            self._audio_bytes += len(chunk)
            offset = next_offset

    async def stop(self, websocket, *, reason: str = "client_stop", notify: bool = True) -> None:
        stream = self._stream
        self._stream = None
        reader_task = self._reader_task
        self._reader_task = None

        if stream is not None:
            try:
                await stream.input_stream.end_stream()
            except Exception:
                pass
        if reader_task is not None:
            try:
                await asyncio.wait_for(reader_task, timeout=5.0)
            except Exception:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        self._state = "stopped"
        if notify and not self._stream_stop_sent:
            await self.send_json(
                websocket,
                {
                    "type": "stream_stopped",
                    "data": self._summary_payload(reason=reason),
                },
            )
            self._stream_stop_sent = True

    async def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                await stream.input_stream.end_stream()
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._reader_task = None
        self._state = "closed"

    def _summary_payload(self, *, reason: str) -> dict[str, Any]:
        return {
            "sessionId": self._session_id,
            "state": self._state,
            "reason": str(reason or "").strip() or "unknown",
            "finalSegments": int(self._final_segments),
            "partialSegments": int(self._partial_segments),
            "audioBytes": int(self._audio_bytes),
            "lastError": str(self._last_error or "").strip(),
        }

    def _resolve_request_config(self, payload: dict[str, Any]) -> AwsRealtimeSttConfig:
        language_code = str(payload.get("languageCode", self._config.language_code) or "").strip()
        media_encoding = str(payload.get("mediaEncoding", self._config.media_encoding) or "").strip().lower()
        partial_results_stability = str(
            payload.get("partialResultsStability", self._config.partial_results_stability) or ""
        ).strip().lower()
        sample_rate_hz = _read_int(
            payload.get("sampleRateHertz", self._config.sample_rate_hz),
            default=self._config.sample_rate_hz,
            minimum=8000,
        )
        if not language_code:
            raise ValueError("languageCode is required")
        if media_encoding not in VALID_MEDIA_ENCODINGS:
            raise ValueError(f"Unsupported mediaEncoding: {media_encoding or '<empty>'}")
        if partial_results_stability not in VALID_PARTIAL_STABILITIES:
            raise ValueError(
                "partialResultsStability must be one of "
                f"{', '.join(sorted(VALID_PARTIAL_STABILITIES))}"
            )
        return AwsRealtimeSttConfig(
            region=self._config.region,
            language_code=language_code,
            media_encoding=media_encoding,
            sample_rate_hz=sample_rate_hz,
            partial_results_stability=partial_results_stability,
            max_audio_chunk_bytes=self._config.max_audio_chunk_bytes,
        )

    async def _forward_transcripts(self, websocket, stream) -> None:
        try:
            async for event in stream.output_stream:
                if TranscriptEvent is None or not isinstance(event, TranscriptEvent):
                    continue
                results = list(getattr(getattr(event, "transcript", None), "results", []) or [])
                for result in results:
                    alternatives = list(getattr(result, "alternatives", []) or [])
                    transcript = ""
                    stable_items = 0
                    items_payload: list[dict[str, Any]] = []
                    if alternatives:
                        alternative = alternatives[0]
                        transcript = str(getattr(alternative, "transcript", "") or "").strip()
                        for item in list(getattr(alternative, "items", []) or []):
                            stable_flag = bool(getattr(item, "stable", False))
                            if stable_flag:
                                stable_items += 1
                            items_payload.append(
                                {
                                    "content": str(getattr(item, "content", "") or "").strip(),
                                    "type": str(getattr(item, "item_type", "") or "").strip(),
                                    "startTime": getattr(item, "start_time", None),
                                    "endTime": getattr(item, "end_time", None),
                                    "confidence": getattr(item, "confidence", None),
                                    "stable": stable_flag,
                                }
                            )
                    if not transcript:
                        continue

                    is_partial = bool(getattr(result, "is_partial", False))
                    if is_partial:
                        self._partial_segments += 1
                    else:
                        self._final_segments += 1

                    await self.send_json(
                        websocket,
                        {
                            "type": "partial" if is_partial else "final",
                            "data": {
                                "sessionId": self._session_id,
                                "resultId": str(getattr(result, "result_id", "") or "").strip(),
                                "isPartial": is_partial,
                                "text": transcript,
                                "startTime": getattr(result, "start_time", None),
                                "endTime": getattr(result, "end_time", None),
                                "channelId": str(getattr(result, "channel_id", "") or "").strip(),
                                "languageCode": str(
                                    getattr(result, "language_code", "") or self._config.language_code
                                ).strip(),
                                "stableItemCount": int(stable_items),
                                "items": items_payload,
                            },
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc).strip() or "AWS Transcribe stream failed"
            self._state = "error"
            await self.send_error(
                websocket,
                stage="aws_transcribe",
                code="TRANSCRIBE_STREAM_FAILED",
                message=self._last_error,
            )
        finally:
            self._stream = None


def _read_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = int(default)
    return max(int(minimum), parsed)
