from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

try:
    from dotenv import dotenv_values, load_dotenv
except Exception:
    dotenv_values = None
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parents[3]
MASTER_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
SDK_SRC = REPO_ROOT / "sdk" / "src"
OPENCLAW_LOCAL_DIR = REPO_ROOT / "Software" / "Master" / "openclaw_local"
MOMO_AGENT_RUNTIME_DIR = PACKAGE_ROOT / "runtime"

REPO_DOTENV_PATHS = tuple(
    path
    for path in (
        REPO_ROOT / ".env",
        REPO_ROOT / "env",
        MASTER_ROOT / ".env",
        MASTER_ROOT / "env",
    )
    if path.exists()
)
if load_dotenv is not None:
    for _dotenv_path in REPO_DOTENV_PATHS:
        load_dotenv(dotenv_path=_dotenv_path, override=False)

GROQ_API_KEY_FALLBACK = os.getenv("GROQ_API_KEY", "")
MIMO_API_KEY_FALLBACK = os.getenv("MIMO_API_KEY", "")

STT_PROVIDER_DEFAULT = "faster-whisper"
GROQ_STT_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL_DEFAULT = "whisper-large-v3"

TTS_PROVIDER_DEFAULT = "mimo"
MIMO_TTS_URL_DEFAULT = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_TTS_MODEL_DEFAULT = "mimo-v2-tts"
MIMO_TTS_VOICE_DEFAULT = "mimo_default"
MIMO_TTS_RESPONSE_FORMAT_DEFAULT = "wav"
MIMO_TTS_TIMEOUT_SEC_DEFAULT = 45.0
MIMO_TTS_MAX_CHARS_DEFAULT = 180

GROQ_TTS_URL_DEFAULT = "https://api.groq.com/openai/v1/audio/speech"
GROQ_TTS_MODEL_DEFAULT = "canopylabs/orpheus-v1-english"
GROQ_TTS_VOICE_DEFAULT = "troy"
GROQ_TTS_RESPONSE_FORMAT_DEFAULT = "wav"
GROQ_TTS_TIMEOUT_SEC_DEFAULT = 45.0
GROQ_TTS_MAX_CHARS_DEFAULT = 180

COSYVOICE_LOCAL_DIR = REPO_ROOT / "Software" / "Master" / "cosyvoice_local"
COSYVOICE_TTS_URL_DEFAULT = "http://127.0.0.1:50000"
COSYVOICE_TTS_MODE_DEFAULT = "zero_shot"
COSYVOICE_TTS_PROMPT_WAV_DEFAULT = str(COSYVOICE_LOCAL_DIR / "assets" / "zero_shot_prompt.wav")
COSYVOICE_TTS_ENDOFPROMPT_TOKEN = "<|endofprompt|>"
COSYVOICE_TTS_TEXT_PREFIX_DEFAULT = f"You are a helpful assistant.{COSYVOICE_TTS_ENDOFPROMPT_TOKEN}"
COSYVOICE_TTS_INSTRUCT_PREFIX_DEFAULT = "You are a helpful assistant. "
COSYVOICE_TTS_PROMPT_TEXT_DEFAULT = (
    f"{COSYVOICE_TTS_TEXT_PREFIX_DEFAULT}希望你以后能够做的比我还好呦。"
)
COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT = (
    f"{COSYVOICE_TTS_INSTRUCT_PREFIX_DEFAULT}{COSYVOICE_TTS_ENDOFPROMPT_TOKEN}"
)
COSYVOICE_TTS_SAMPLE_RATE_DEFAULT = 24000
COSYVOICE_TTS_TIMEOUT_SEC_DEFAULT = 90.0
COSYVOICE_TTS_MAX_CHARS_DEFAULT = 120

OPENCLAW_BIN_DEFAULT = "openclaw"
OPENCLAW_AGENT_ID_DEFAULT = "main"
OPENCLAW_TIMEOUT_SEC_DEFAULT = 90.0
OPENCLAW_SKILL_NAME_DEFAULT = "soarmmoce-control"
OPENCLAW_GATEWAY_BRIDGE_SCRIPT_DEFAULT = OPENCLAW_LOCAL_DIR / "openclaw_gateway_bridge.js"
OPENCLAW_THINKING_DEFAULT = "minimal"

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_MAX_RECORD_SEC = 20.0


def _runtime_env_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if dotenv_values is None:
        return values
    for dotenv_path in REPO_DOTENV_PATHS:
        try:
            payload = dotenv_values(dotenv_path)
        except Exception:
            continue
        for key, value in payload.items():
            if key and value is not None:
                values[str(key)] = str(value)
    return values


def _runtime_env_get(
    name: str,
    default: Optional[str] = None,
    env_values: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    if env_values is not None and name in env_values:
        return str(env_values[name])
    current = os.getenv(name)
    if current is not None:
        return str(current)
    return default


def _runtime_env_bool(
    name: str,
    default: bool,
    env_values: Optional[Dict[str, str]] = None,
) -> bool:
    raw = _runtime_env_get(name, None, env_values)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _normalize_stt_provider(value: Optional[str]) -> str:
    provider = str(value or "").strip().lower() or STT_PROVIDER_DEFAULT
    if provider in {"groq", "faster-whisper", "openai-compatible", "local"}:
        return provider
    return STT_PROVIDER_DEFAULT


def _normalize_tts_provider(value: Optional[str]) -> str:
    provider = str(value or "").strip().lower() or TTS_PROVIDER_DEFAULT
    if provider in {"mimo", "groq", "cosyvoice"}:
        return provider
    return TTS_PROVIDER_DEFAULT


def _normalize_cosyvoice_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().lower() or COSYVOICE_TTS_MODE_DEFAULT
    if mode in {"cross_lingual", "zero_shot", "instruct2"}:
        return mode
    return COSYVOICE_TTS_MODE_DEFAULT


def _read_float(value: Optional[str], default: float, minimum: Optional[float] = None) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = float(default)
    if minimum is not None:
        return max(float(minimum), parsed)
    return parsed


def _read_int(value: Optional[str], default: int, minimum: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = int(default)
    if minimum is not None:
        return max(int(minimum), parsed)
    return parsed


@dataclass
class AudioConfig:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    max_record_sec: float = DEFAULT_MAX_RECORD_SEC


@dataclass
class SttConfig:
    provider: str = STT_PROVIDER_DEFAULT
    api_key: str = ""
    url: str = GROQ_STT_URL_DEFAULT
    model: str = GROQ_STT_MODEL_DEFAULT
    language: str = "zh"
    timeout_sec: float = 45.0


@dataclass
class TtsConfig:
    enabled: bool = True
    provider: str = TTS_PROVIDER_DEFAULT
    playback_backend: str = "auto"
    api_key: str = ""
    url: str = MIMO_TTS_URL_DEFAULT
    model: str = MIMO_TTS_MODEL_DEFAULT
    voice: str = MIMO_TTS_VOICE_DEFAULT
    response_format: str = MIMO_TTS_RESPONSE_FORMAT_DEFAULT
    timeout_sec: float = MIMO_TTS_TIMEOUT_SEC_DEFAULT
    max_chars: int = MIMO_TTS_MAX_CHARS_DEFAULT
    groq_api_key: str = ""
    groq_url: str = GROQ_TTS_URL_DEFAULT
    groq_model: str = GROQ_TTS_MODEL_DEFAULT
    groq_voice: str = GROQ_TTS_VOICE_DEFAULT
    groq_response_format: str = GROQ_TTS_RESPONSE_FORMAT_DEFAULT
    groq_timeout_sec: float = GROQ_TTS_TIMEOUT_SEC_DEFAULT
    groq_max_chars: int = GROQ_TTS_MAX_CHARS_DEFAULT
    cosyvoice_url: str = COSYVOICE_TTS_URL_DEFAULT
    cosyvoice_mode: str = COSYVOICE_TTS_MODE_DEFAULT
    cosyvoice_prompt_wav: str = COSYVOICE_TTS_PROMPT_WAV_DEFAULT
    cosyvoice_prompt_text: str = COSYVOICE_TTS_PROMPT_TEXT_DEFAULT
    cosyvoice_instruct_text: str = COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT
    cosyvoice_sample_rate: int = COSYVOICE_TTS_SAMPLE_RATE_DEFAULT
    cosyvoice_timeout_sec: float = COSYVOICE_TTS_TIMEOUT_SEC_DEFAULT
    cosyvoice_max_chars: int = COSYVOICE_TTS_MAX_CHARS_DEFAULT


@dataclass
class OpenClawConfig:
    enabled: bool = True
    binary: str = OPENCLAW_BIN_DEFAULT
    agent_id: str = OPENCLAW_AGENT_ID_DEFAULT
    skill_name: str = OPENCLAW_SKILL_NAME_DEFAULT
    local_mode: bool = False
    robot_mode: bool = True
    force_new_session: bool = False
    node_retry_count: int = 2
    thinking: str = OPENCLAW_THINKING_DEFAULT
    timeout_sec: float = OPENCLAW_TIMEOUT_SEC_DEFAULT
    session_id: str = ""
    gateway_bridge_enabled: bool = True
    gateway_bridge_script: str = str(OPENCLAW_GATEWAY_BRIDGE_SCRIPT_DEFAULT)
    node_bin: str = ""


@dataclass
class MomoAgentConfig:
    audio: AudioConfig
    stt: SttConfig
    tts: TtsConfig
    openclaw: OpenClawConfig


def load_config() -> MomoAgentConfig:
    runtime_env = _runtime_env_values()

    audio = AudioConfig(
        sample_rate=_read_int(
            _runtime_env_get("MOMO_AGENT_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE), runtime_env),
            DEFAULT_SAMPLE_RATE,
            minimum=8000,
        ),
        channels=_read_int(
            _runtime_env_get("MOMO_AGENT_CHANNELS", str(DEFAULT_CHANNELS), runtime_env),
            DEFAULT_CHANNELS,
            minimum=1,
        ),
        max_record_sec=_read_float(
            _runtime_env_get("MOMO_AGENT_MAX_RECORD_SEC", str(DEFAULT_MAX_RECORD_SEC), runtime_env),
            DEFAULT_MAX_RECORD_SEC,
            minimum=1.0,
        ),
    )

    stt_api_key = _runtime_env_get("SOARMMOCE_STT_API_KEY", None, runtime_env)
    if stt_api_key is None:
        stt_api_key = (
            _runtime_env_get("GROQ_API_KEY", None, runtime_env) or GROQ_API_KEY_FALLBACK or ""
        )
    stt = SttConfig(
        provider=_normalize_stt_provider(
            _runtime_env_get("SOARMMOCE_STT_PROVIDER", STT_PROVIDER_DEFAULT, runtime_env)
        ),
        api_key=str(stt_api_key or "").strip(),
        url=(
            _runtime_env_get("SOARMMOCE_STT_URL", None, runtime_env)
            or _runtime_env_get("GROQ_STT_URL", None, runtime_env)
            or GROQ_STT_URL_DEFAULT
        ).strip()
        or GROQ_STT_URL_DEFAULT,
        model=(
            _runtime_env_get("SOARMMOCE_STT_MODEL", None, runtime_env)
            or _runtime_env_get("GROQ_STT_MODEL", None, runtime_env)
            or GROQ_STT_MODEL_DEFAULT
        ).strip()
        or GROQ_STT_MODEL_DEFAULT,
        language=(
            _runtime_env_get("SOARMMOCE_STT_LANGUAGE", None, runtime_env)
            or _runtime_env_get("GROQ_STT_LANGUAGE", None, runtime_env)
            or "zh"
        ).strip()
        or "zh",
        timeout_sec=_read_float(
            _runtime_env_get("SOARMMOCE_STT_TIMEOUT_SEC", None, runtime_env)
            or _runtime_env_get("GROQ_STT_TIMEOUT_SEC", None, runtime_env)
            or "45.0",
            45.0,
            minimum=5.0,
        ),
    )

    tts_provider = _normalize_tts_provider(
        _runtime_env_get("SOARMMOCE_TTS_PROVIDER", TTS_PROVIDER_DEFAULT, runtime_env)
    )
    tts_api_key = str(
        _runtime_env_get("SOARMMOCE_TTS_API_KEY", None, runtime_env)
        or _runtime_env_get("MIMO_TTS_API_KEY", None, runtime_env)
        or _runtime_env_get("MIMO_API_KEY", None, runtime_env)
        or MIMO_API_KEY_FALLBACK
        or ""
    ).strip()
    groq_tts_api_key = str(
        _runtime_env_get("SOARMMOCE_TTS_API_KEY", None, runtime_env)
        or _runtime_env_get("GROQ_TTS_API_KEY", None, runtime_env)
        or _runtime_env_get("GROQ_API_KEY", None, runtime_env)
        or GROQ_API_KEY_FALLBACK
        or ""
    ).strip()
    tts = TtsConfig(
        enabled=_runtime_env_bool("SOARMMOCE_TTS_ENABLED", True, runtime_env),
        provider=tts_provider,
        playback_backend=(
            _runtime_env_get("SOARMMOCE_TTS_PLAYBACK_BACKEND", "auto", runtime_env) or "auto"
        ).strip()
        or "auto",
        api_key=tts_api_key,
        url=(
            _runtime_env_get("SOARMMOCE_TTS_URL", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_URL", None, runtime_env)
            or MIMO_TTS_URL_DEFAULT
        ).strip()
        or MIMO_TTS_URL_DEFAULT,
        model=(
            _runtime_env_get("SOARMMOCE_TTS_MODEL", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_MODEL", None, runtime_env)
            or MIMO_TTS_MODEL_DEFAULT
        ).strip()
        or MIMO_TTS_MODEL_DEFAULT,
        voice=(
            _runtime_env_get("SOARMMOCE_TTS_VOICE", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_VOICE", None, runtime_env)
            or MIMO_TTS_VOICE_DEFAULT
        ).strip()
        or MIMO_TTS_VOICE_DEFAULT,
        response_format=(
            _runtime_env_get("SOARMMOCE_TTS_RESPONSE_FORMAT", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_RESPONSE_FORMAT", None, runtime_env)
            or MIMO_TTS_RESPONSE_FORMAT_DEFAULT
        ).strip()
        or MIMO_TTS_RESPONSE_FORMAT_DEFAULT,
        timeout_sec=_read_float(
            _runtime_env_get("SOARMMOCE_TTS_TIMEOUT_SEC", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_TIMEOUT_SEC", None, runtime_env)
            or str(MIMO_TTS_TIMEOUT_SEC_DEFAULT),
            MIMO_TTS_TIMEOUT_SEC_DEFAULT,
            minimum=5.0,
        ),
        max_chars=_read_int(
            _runtime_env_get("SOARMMOCE_TTS_MAX_CHARS", None, runtime_env)
            or _runtime_env_get("MIMO_TTS_MAX_CHARS", None, runtime_env)
            or str(MIMO_TTS_MAX_CHARS_DEFAULT),
            MIMO_TTS_MAX_CHARS_DEFAULT,
            minimum=32,
        ),
        groq_api_key=groq_tts_api_key,
        groq_url=(
            _runtime_env_get("SOARMMOCE_TTS_URL", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_URL", None, runtime_env)
            or GROQ_TTS_URL_DEFAULT
        ).strip()
        or GROQ_TTS_URL_DEFAULT,
        groq_model=(
            _runtime_env_get("SOARMMOCE_TTS_MODEL", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_MODEL", None, runtime_env)
            or GROQ_TTS_MODEL_DEFAULT
        ).strip()
        or GROQ_TTS_MODEL_DEFAULT,
        groq_voice=(
            _runtime_env_get("SOARMMOCE_TTS_VOICE", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_VOICE", None, runtime_env)
            or GROQ_TTS_VOICE_DEFAULT
        ).strip()
        or GROQ_TTS_VOICE_DEFAULT,
        groq_response_format=(
            _runtime_env_get("SOARMMOCE_TTS_RESPONSE_FORMAT", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_RESPONSE_FORMAT", None, runtime_env)
            or GROQ_TTS_RESPONSE_FORMAT_DEFAULT
        ).strip()
        or GROQ_TTS_RESPONSE_FORMAT_DEFAULT,
        groq_timeout_sec=_read_float(
            _runtime_env_get("SOARMMOCE_TTS_TIMEOUT_SEC", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_TIMEOUT_SEC", None, runtime_env)
            or str(GROQ_TTS_TIMEOUT_SEC_DEFAULT),
            GROQ_TTS_TIMEOUT_SEC_DEFAULT,
            minimum=5.0,
        ),
        groq_max_chars=_read_int(
            _runtime_env_get("SOARMMOCE_TTS_MAX_CHARS", None, runtime_env)
            or _runtime_env_get("GROQ_TTS_MAX_CHARS", None, runtime_env)
            or str(GROQ_TTS_MAX_CHARS_DEFAULT),
            GROQ_TTS_MAX_CHARS_DEFAULT,
            minimum=32,
        ),
        cosyvoice_url=(
            _runtime_env_get("SOARMMOCE_COSYVOICE_URL", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_URL", None, runtime_env)
            or COSYVOICE_TTS_URL_DEFAULT
        ).strip()
        or COSYVOICE_TTS_URL_DEFAULT,
        cosyvoice_mode=_normalize_cosyvoice_mode(
            _runtime_env_get("SOARMMOCE_COSYVOICE_MODE", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_MODE", None, runtime_env)
            or COSYVOICE_TTS_MODE_DEFAULT
        ),
        cosyvoice_prompt_wav=str(
            _runtime_env_get("SOARMMOCE_COSYVOICE_PROMPT_WAV", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_PROMPT_WAV", None, runtime_env)
            or COSYVOICE_TTS_PROMPT_WAV_DEFAULT
        ).strip()
        or COSYVOICE_TTS_PROMPT_WAV_DEFAULT,
        cosyvoice_prompt_text=str(
            _runtime_env_get("SOARMMOCE_COSYVOICE_PROMPT_TEXT", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_PROMPT_TEXT", None, runtime_env)
            or COSYVOICE_TTS_PROMPT_TEXT_DEFAULT
        ).strip()
        or COSYVOICE_TTS_PROMPT_TEXT_DEFAULT,
        cosyvoice_instruct_text=str(
            _runtime_env_get("SOARMMOCE_COSYVOICE_INSTRUCT_TEXT", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_INSTRUCT_TEXT", None, runtime_env)
            or COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT
        ).strip()
        or COSYVOICE_TTS_INSTRUCT_TEXT_DEFAULT,
        cosyvoice_sample_rate=_read_int(
            _runtime_env_get("SOARMMOCE_COSYVOICE_SAMPLE_RATE", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_SAMPLE_RATE", None, runtime_env)
            or str(COSYVOICE_TTS_SAMPLE_RATE_DEFAULT),
            COSYVOICE_TTS_SAMPLE_RATE_DEFAULT,
            minimum=8000,
        ),
        cosyvoice_timeout_sec=_read_float(
            _runtime_env_get("SOARMMOCE_COSYVOICE_TIMEOUT_SEC", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_TIMEOUT_SEC", None, runtime_env)
            or _runtime_env_get("SOARMMOCE_TTS_TIMEOUT_SEC", None, runtime_env)
            or str(COSYVOICE_TTS_TIMEOUT_SEC_DEFAULT),
            COSYVOICE_TTS_TIMEOUT_SEC_DEFAULT,
            minimum=5.0,
        ),
        cosyvoice_max_chars=_read_int(
            _runtime_env_get("SOARMMOCE_COSYVOICE_MAX_CHARS", None, runtime_env)
            or _runtime_env_get("COSYVOICE_TTS_MAX_CHARS", None, runtime_env)
            or _runtime_env_get("SOARMMOCE_TTS_MAX_CHARS", None, runtime_env)
            or str(COSYVOICE_TTS_MAX_CHARS_DEFAULT),
            COSYVOICE_TTS_MAX_CHARS_DEFAULT,
            minimum=32,
        ),
    )

    openclaw = OpenClawConfig(
        enabled=_runtime_env_bool("OPENCLAW_ENABLED", True, runtime_env),
        binary=(os.getenv("OPENCLAW_BIN", OPENCLAW_BIN_DEFAULT).strip() or OPENCLAW_BIN_DEFAULT),
        agent_id=(
            os.getenv("OPENCLAW_AGENT_ID", OPENCLAW_AGENT_ID_DEFAULT).strip()
            or OPENCLAW_AGENT_ID_DEFAULT
        ),
        skill_name=(
            os.getenv("OPENCLAW_SKILL_NAME", OPENCLAW_SKILL_NAME_DEFAULT).strip()
            or OPENCLAW_SKILL_NAME_DEFAULT
        ),
        local_mode=_runtime_env_bool("OPENCLAW_LOCAL", False, runtime_env),
        robot_mode=_runtime_env_bool("OPENCLAW_ROBOT_MODE", True, runtime_env),
        force_new_session=_runtime_env_bool("OPENCLAW_FORCE_NEW_SESSION", False, runtime_env),
        node_retry_count=_read_int(
            _runtime_env_get("OPENCLAW_NODE_RETRY_COUNT", "2", runtime_env),
            2,
            minimum=0,
        ),
        thinking=(
            os.getenv("OPENCLAW_THINKING", OPENCLAW_THINKING_DEFAULT).strip()
            or OPENCLAW_THINKING_DEFAULT
        ),
        timeout_sec=_read_float(
            _runtime_env_get("OPENCLAW_TIMEOUT_SEC", str(OPENCLAW_TIMEOUT_SEC_DEFAULT), runtime_env),
            OPENCLAW_TIMEOUT_SEC_DEFAULT,
            minimum=5.0,
        ),
        session_id=str(os.getenv("OPENCLAW_SESSION_ID", "")).strip(),
        gateway_bridge_enabled=_runtime_env_bool("OPENCLAW_GATEWAY_BRIDGE_ENABLED", True, runtime_env),
        gateway_bridge_script=str(
            _runtime_env_get(
                "OPENCLAW_GATEWAY_BRIDGE_SCRIPT",
                str(OPENCLAW_GATEWAY_BRIDGE_SCRIPT_DEFAULT),
                runtime_env,
            )
            or OPENCLAW_GATEWAY_BRIDGE_SCRIPT_DEFAULT
        ).strip(),
        node_bin=str(_runtime_env_get("OPENCLAW_NODE_BIN", "", runtime_env) or "").strip(),
    )

    return MomoAgentConfig(audio=audio, stt=stt, tts=tts, openclaw=openclaw)
