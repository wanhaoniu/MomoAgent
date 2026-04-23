from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_client import AgentReply, ToolRequester
from .config import MOMO_AGENT_RUNTIME_DIR, REPO_ROOT, NanobotConfig

SESSION_STATE_PATH = MOMO_AGENT_RUNTIME_DIR / "nanobot_session_state.json"
MASTER_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DISPATCHER_PATH = MASTER_ROOT / "hmi" / "skills_dispatcher.py"
DEFAULT_NANOBOT_SOURCE_DIR = REPO_ROOT / "external" / "nanobot"
SUPPORTED_TOOL_NAMES = frozenset(
    {
        "move_robot_arm",
        "get_robot_state",
        "stop_robot",
        "set_gripper",
        "rotate_joint",
        "run_robot_behavior",
    }
)
_SKILLS_DISPATCHER_MODULE: Any | None = None


def _normalized_tool_mode(config: NanobotConfig) -> str:
    mode = str(getattr(config, "tool_mode", "") or "").strip().lower()
    if mode in {"bridge", "bridge-only", "bridge_only", "robot-only", "robot_only"}:
        return "bridge_only"
    if mode in {"all", "full", "full-access", "full_access"}:
        return "all"
    return "hybrid"


def _tool_mode_keeps_default_tools(config: NanobotConfig) -> bool:
    return _normalized_tool_mode(config) != "bridge_only"


def _disabled_skill_names_signature(config: NanobotConfig) -> str:
    names = _build_disabled_skill_names(config)
    return ",".join(sorted(str(name).strip() for name in names if str(name).strip()))


def _effective_tool_runtime(config: NanobotConfig) -> dict[str, bool]:
    mode = _normalized_tool_mode(config)
    if mode == "all":
        return {
            "enable_exec": True,
            "enable_web": True,
            "enable_my_tool": True,
            "restrict_to_workspace": False,
        }
    return {
        "enable_exec": bool(config.enable_exec),
        "enable_web": bool(config.enable_web),
        "enable_my_tool": bool(config.enable_my_tool),
        "restrict_to_workspace": bool(config.restrict_to_workspace),
    }


def _load_skills_dispatcher_module() -> Any:
    global _SKILLS_DISPATCHER_MODULE
    if _SKILLS_DISPATCHER_MODULE is not None:
        return _SKILLS_DISPATCHER_MODULE
    if not SKILLS_DISPATCHER_PATH.is_file():
        raise RuntimeError(f"skills_dispatcher.py not found: {SKILLS_DISPATCHER_PATH}")
    spec = importlib.util.spec_from_file_location(
        "momo_hmi_skills_dispatcher_runtime",
        str(SKILLS_DISPATCHER_PATH),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load skills_dispatcher.py: {SKILLS_DISPATCHER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SKILLS_DISPATCHER_MODULE = module
    return module


def _get_dispatcher_types() -> tuple[type[Any], list[dict[str, Any]]]:
    module = _load_skills_dispatcher_module()
    dispatcher_cls = getattr(module, "LocalToolDispatcher", None)
    tool_schemas = getattr(module, "OPENAI_TOOL_SCHEMAS", None)
    if not isinstance(tool_schemas, list) or dispatcher_cls is None:
        raise RuntimeError("skills_dispatcher.py did not expose expected dispatcher symbols")
    return dispatcher_cls, list(tool_schemas)


def _resolve_nanobot_source_dir(config: NanobotConfig | None = None) -> Path | None:
    raw = ""
    if config is not None:
        raw = str(config.source_dir or "").strip()
    if not raw:
        raw = str(os.getenv("MOMO_AGENT_NANOBOT_SOURCE_DIR", "")).strip()
    candidate = Path(raw or DEFAULT_NANOBOT_SOURCE_DIR).expanduser().resolve()
    if not (candidate / "nanobot" / "__init__.py").is_file():
        return None
    return candidate


def _ensure_nanobot_source_path(config: NanobotConfig | None = None) -> Path | None:
    source_dir = _resolve_nanobot_source_dir(config)
    if source_dir is None:
        return None
    normalized = os.path.normpath(str(source_dir))
    sys.path[:] = [
        path for path in sys.path if os.path.normpath(path or os.curdir) != normalized
    ]
    sys.path.insert(0, str(source_dir))
    return source_dir


def _import_nanobot_modules(config: NanobotConfig | None = None):
    if sys.version_info < (3, 11):
        raise RuntimeError("Nanobot backend requires Python 3.11+.")
    source_dir = _ensure_nanobot_source_path(config)
    try:
        from nanobot import Nanobot
        from nanobot.agent import AgentHook, AgentHookContext
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.base import Tool
    except Exception as exc:  # noqa: BLE001
        source_hint = f" from {source_dir}" if source_dir is not None else ""
        raise RuntimeError(
            "Nanobot backend is unavailable. Install `nanobot-ai` on Python 3.11+ "
            "or bootstrap the vendored clone with `bash scripts/bootstrap_nanobot.sh`"
            f"{source_hint}, or switch MOMO_AGENT_BACKEND back to `openclaw`."
        ) from exc
    return Nanobot, AgentHook, AgentHookContext, BUILTIN_SKILLS_DIR, Tool


def _supported_tool_schemas() -> list[dict[str, Any]]:
    _, all_schemas = _get_dispatcher_types()
    out: list[dict[str, Any]] = []
    for schema in all_schemas:
        fn = schema.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name", "")).strip()
        if name and name in SUPPORTED_TOOL_NAMES:
            out.append(schema)
    return out


def _build_disabled_skill_names(config: NanobotConfig) -> list[str]:
    names: list[str] = []
    if config.disable_builtin_skills:
        try:
            _, _, _, builtin_dir, _ = _import_nanobot_modules(config)
            if builtin_dir.exists():
                for skill_dir in builtin_dir.iterdir():
                    if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                        names.append(skill_dir.name)
        except Exception:
            pass
    for skill_name in config.disabled_skills:
        if skill_name not in names:
            names.append(skill_name)
    return names


def _write_if_missing(path: Path, content: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ensure_workspace_files(config: NanobotConfig) -> None:
    workspace = Path(config.workspace).expanduser().resolve()
    memory_dir = workspace / "memory"
    skill_dir = workspace / "skills" / config.skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    _write_if_missing(
        workspace / "AGENTS.md",
        """# MomoAgent Instructions

You are the local robot interaction layer for MomoAgent.

- Use only the registered robot tools for physical actions.
- Use nanobot builtin skills by reading their `SKILL.md` instructions or following the skills summary; do not route builtin skills through robot behavior tools.
- Do not invent robot state, camera observations, or execution success.
- If location, object identity, or target joint is ambiguous, ask one concise clarifying question.
- If a robot tool reports that the arm is not connected or unavailable, explain the issue briefly and ask the user how they want to proceed.
- After tool calls, summarize the real result in plain language.
""",
    )
    _write_if_missing(
        workspace / "SOUL.md",
        """# Soul

You are Momo, a calm and practical robot assistant.

- Match the user's language.
- Keep responses short unless the user asks for depth.
- Prefer concrete action over vague promises.
""",
    )
    _write_if_missing(
        workspace / "USER.md",
        """# User

Stable user preferences and interaction style will be recorded here over time.
""",
    )
    _write_if_missing(
        memory_dir / "MEMORY.md",
        """# Memory

- This workspace belongs to the MomoAgent robot control bridge.
- Use robot tools for physical actions and real state inspection.
- Skill and SDK layers enforce the low-level motion limits.
""",
    )
    _write_if_missing(
        workspace / "TOOLS.md",
        """# Tool Guidance

Primary robot tools:

- `get_robot_state` for current joints and TCP state
- `move_robot_arm` for Cartesian moves
- `rotate_joint` for joint-level adjustment
- `set_gripper` for gripper open ratio
- `stop_robot` for immediate stop
- `run_robot_behavior` for higher-level curated robot behaviors when available
- Never use `run_robot_behavior` for nanobot builtin skills such as `weather`, `github`, `tmux`, `summarize`, or `clawhub`
""",
    )
    _write_if_missing(
        skill_dir / "SKILL.md",
        f"""---
description: Use the robot control bridge tools to inspect and move the Momo robot arm.
metadata:
  nanobot:
    always: true
---

# {config.skill_name}

Use these tools when the user asks about the robot arm:

- Start with `get_robot_state` when you need current pose, joints, or gripper status.
- Use `move_robot_arm` only when the user clearly specifies a Cartesian target.
- Use `rotate_joint` when the user names a joint or asks for a simple incremental adjustment.
- Use `set_gripper` for open and close style requests.
- Use `stop_robot` immediately if the user asks to stop.
- Use `run_robot_behavior` only for explicitly named, higher-level robot behaviors that are implemented.
- Do not use `run_robot_behavior` for nanobot builtin skills like `weather`, `github`, `tmux`, or `summarize`.

If the request is ambiguous, ask a short clarification question instead of guessing.
""",
    )


def _write_runtime_config(config: NanobotConfig) -> Path:
    config_path = Path(config.config_path).expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tool_runtime = _effective_tool_runtime(config)
    payload = {
        "providers": {
            str(config.provider): {
                "apiKey": str(config.api_key or ""),
                "apiBase": str(config.api_base or "").strip(),
            }
        },
        "agents": {
            "defaults": {
                "workspace": str(Path(config.workspace).expanduser().resolve()),
                "provider": str(config.provider or "custom").strip() or "custom",
                "model": str(config.model or "").strip(),
                "maxToolIterations": int(config.max_tool_iterations),
                "contextWindowTokens": int(config.context_window_tokens),
                "maxToolResultChars": int(config.max_tool_result_chars),
                "temperature": float(config.temperature),
                "providerRetryMode": str(config.provider_retry_mode or "standard").strip()
                or "standard",
                "reasoningEffort": str(config.reasoning_effort or "").strip() or None,
                "timezone": time.tzname[0] if time.tzname else "UTC",
                "disabledSkills": _build_disabled_skill_names(config),
                "idleCompactAfterMinutes": int(config.session_ttl_minutes),
                "dream": {
                    "intervalH": int(config.dream_interval_h),
                    "maxBatchSize": int(config.dream_max_batch_size),
                    "maxIterations": int(config.dream_max_iterations),
                    "modelOverride": str(config.dream_model_override or "").strip() or None,
                },
            }
        },
        "tools": {
            "web": {"enable": bool(tool_runtime["enable_web"])},
            "exec": {"enable": bool(tool_runtime["enable_exec"])},
            "my": {"enable": bool(tool_runtime["enable_my_tool"]), "allowSet": False},
            "restrictToWorkspace": bool(tool_runtime["restrict_to_workspace"]),
        },
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


@dataclass
class _ToolCallRecord:
    name: str
    arguments: dict[str, Any]


class NanobotBridgeClient:
    def __init__(
        self,
        config: NanobotConfig,
        *,
        tool_requester: ToolRequester | None = None,
        tool_request_timeout_sec: float = 6.0,
    ) -> None:
        self._config = config
        self._lock = threading.RLock()
        dispatcher_cls, _ = _get_dispatcher_types()
        self._dispatcher = dispatcher_cls(
            tool_requester=tool_requester,
            tool_request_timeout_sec=tool_request_timeout_sec,
        )
        _ensure_workspace_files(config)
        self._config_path = _write_runtime_config(config)
        self._session_id = self._resolve_initial_session_id()
        self._bridge_session_key = f"nanobot:{self._session_id}"
        self._bot = self._build_bot()

    def _build_bot(self):
        Nanobot, _, _, _, Tool = _import_nanobot_modules(self._config)
        bot = Nanobot.from_config(
            config_path=str(self._config_path),
            workspace=str(Path(self._config.workspace).expanduser().resolve()),
        )

        if not _tool_mode_keeps_default_tools(self._config):
            # Narrow mode keeps only curated robot tools.
            for tool_name in list(bot._loop.tools.tool_names):
                bot._loop.tools.unregister(tool_name)

        class _DispatcherTool(Tool):
            def __init__(self, schema: dict[str, Any], dispatcher: Any) -> None:
                self._schema = schema
                self._dispatcher = dispatcher
                fn = schema["function"]
                self._name = str(fn["name"])
                self._description = str(fn.get("description", "") or "")
                self._parameters = dict(fn.get("parameters") or {})

            @property
            def name(self) -> str:
                return self._name

            @property
            def description(self) -> str:
                return self._description

            @property
            def parameters(self) -> dict[str, Any]:
                return self._parameters

            @property
            def read_only(self) -> bool:
                return self._name in {"get_robot_state"}

            async def execute(self, **kwargs: Any) -> Any:
                return self._dispatcher.dispatch(self._name, kwargs)

        for schema in _supported_tool_schemas():
            bot._loop.tools.register(_DispatcherTool(schema, self._dispatcher))
        return bot

    def _state_identity(self) -> dict[str, str]:
        tool_runtime = _effective_tool_runtime(self._config)
        return {
            "workspace": str(Path(self._config.workspace).expanduser().resolve()),
            "provider": str(self._config.provider or "").strip(),
            "model": str(self._config.model or "").strip(),
            "skill_name": str(self._config.skill_name or "").strip(),
            "tool_mode": _normalized_tool_mode(self._config),
            "enable_exec": "1" if tool_runtime["enable_exec"] else "0",
            "enable_web": "1" if tool_runtime["enable_web"] else "0",
            "enable_my_tool": "1" if tool_runtime["enable_my_tool"] else "0",
            "restrict_to_workspace": "1" if tool_runtime["restrict_to_workspace"] else "0",
            "disable_builtin_skills": "1" if self._config.disable_builtin_skills else "0",
            "disabled_skills": _disabled_skill_names_signature(self._config),
        }

    def _load_persisted_state(self) -> dict[str, Any] | None:
        try:
            if not SESSION_STATE_PATH.is_file():
                return None
            payload = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("identity") != self._state_identity():
            return None
        return payload

    def _has_persisted_identity_mismatch(self) -> bool:
        try:
            if not SESSION_STATE_PATH.is_file():
                return False
            payload = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("identity") != self._state_identity()

    def _persist_state(self) -> None:
        MOMO_AGENT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "identity": self._state_identity(),
            "session_id": self._session_id,
            "updated_at": time.time(),
        }
        SESSION_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _make_session_id(self) -> str:
        prefix = str(self._config.session_key_prefix or "momo-agent").strip() or "momo-agent"
        return f"{prefix}:{uuid.uuid4().hex[:8]}"

    def _resolve_initial_session_id(self) -> str:
        configured = str(self._config.session_key or "").strip()
        if configured and not self._config.force_new_session:
            return configured
        if not self._config.force_new_session and self._has_persisted_identity_mismatch():
            return self._make_session_id()
        persisted = None if self._config.force_new_session else self._load_persisted_state()
        persisted_session = str((persisted or {}).get("session_id", "")).strip()
        if persisted_session:
            return persisted_session
        if configured:
            return configured
        if self._config.force_new_session:
            return self._make_session_id()
        prefix = str(self._config.session_key_prefix or "momo-agent").strip() or "momo-agent"
        return f"{prefix}:main"

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def bridge_session_key(self) -> str:
        return self._bridge_session_key

    def close(self) -> None:
        return

    def reset_session(self) -> None:
        with self._lock:
            sessions = self._bot._loop.sessions
            sessions.delete_session(self._session_id)
            self._session_id = self._make_session_id()
            self._bridge_session_key = f"nanobot:{self._session_id}"
            if not self._config.force_new_session:
                self._persist_state()

    def update_session_state(self, *, session_id: str = "", bridge_session_key: str = "") -> None:
        next_session_id = str(session_id or "").strip()
        next_bridge_key = str(bridge_session_key or "").strip()
        if next_session_id:
            self._session_id = next_session_id
        if next_bridge_key:
            self._bridge_session_key = next_bridge_key
        else:
            self._bridge_session_key = f"nanobot:{self._session_id}"
        if not self._config.force_new_session:
            self._persist_state()

    def ask(self, message: str) -> AgentReply:
        text = str(message or "").strip()
        if not text:
            raise RuntimeError("Nanobot input is empty")

        _, AgentHook, AgentHookContext, _, _ = _import_nanobot_modules(self._config)
        records: list[_ToolCallRecord] = []

        class _AuditHook(AgentHook):
            async def before_execute_tools(self, context: AgentHookContext) -> None:
                for tool_call in context.tool_calls:
                    records.append(
                        _ToolCallRecord(
                            name=str(tool_call.name),
                            arguments=dict(tool_call.arguments or {}),
                        )
                    )

        async def _run_once() -> AgentReply:
            result = await asyncio.wait_for(
                self._bot.run(text, session_key=self._session_id, hooks=[_AuditHook()]),
                timeout=self._config.timeout_sec,
            )
            reply_text = str(result.content or "").strip()
            if not reply_text:
                raise RuntimeError("Nanobot returned an empty reply")
            payload = {
                "backend": "nanobot",
                "session_key": self._session_id,
                "tool_calls": [
                    {"name": record.name, "arguments": record.arguments}
                    for record in records
                ],
                "workspace": str(Path(self._config.workspace).expanduser().resolve()),
                "config_path": str(self._config_path),
            }
            return AgentReply(
                text=reply_text,
                session_id=self._session_id,
                raw_payload=payload,
            )

        with self._lock:
            try:
                reply = asyncio.run(_run_once())
            except TimeoutError as exc:  # pragma: no cover - defensive
                raise RuntimeError(
                    f"Nanobot timed out after {float(self._config.timeout_sec):.1f}s"
                ) from exc
            if not self._config.force_new_session:
                self._persist_state()
            return reply


_NANOBOT_CLIENTS: list[NanobotBridgeClient] = []


def build_nanobot_client(
    config: NanobotConfig,
    *,
    tool_requester: ToolRequester | None = None,
    tool_request_timeout_sec: float = 6.0,
) -> NanobotBridgeClient:
    client = NanobotBridgeClient(
        config,
        tool_requester=tool_requester,
        tool_request_timeout_sec=tool_request_timeout_sec,
    )
    _NANOBOT_CLIENTS.append(client)
    return client
