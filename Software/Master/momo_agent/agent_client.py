from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import MomoAgentConfig

ToolRequester = Callable[[str, dict[str, Any], str, float], dict[str, Any]]


@dataclass
class AgentReply:
    text: str
    session_id: str
    raw_payload: Any


class AgentClient(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def bridge_session_key(self) -> str: ...

    def ask(self, message: str) -> AgentReply: ...

    def close(self) -> None: ...

    def reset_session(self) -> None: ...

    def update_session_state(self, *, session_id: str = "", bridge_session_key: str = "") -> None: ...


def build_agent_client(
    config: MomoAgentConfig,
    *,
    tool_requester: ToolRequester | None = None,
    tool_request_timeout_sec: float = 6.0,
) -> AgentClient:
    backend = str(config.agent_backend or "openclaw").strip().lower() or "openclaw"
    if backend == "nanobot":
        from .nanobot_client import build_nanobot_client

        return build_nanobot_client(
            config.nanobot,
            tool_requester=tool_requester,
            tool_request_timeout_sec=tool_request_timeout_sec,
        )

    from .openclaw_client import build_openclaw_client

    return build_openclaw_client(config.openclaw)
