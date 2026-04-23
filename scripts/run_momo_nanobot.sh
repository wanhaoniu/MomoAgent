#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv-nanobot/bin/python"
NANOBOT_DIR="${REPO_ROOT}/external/nanobot"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "[run-momo-nanobot] Missing ${VENV_PYTHON}" >&2
  echo "[run-momo-nanobot] Run 'bash scripts/bootstrap_nanobot.sh' first." >&2
  exit 1
fi

export MOMO_AGENT_BACKEND="${MOMO_AGENT_BACKEND:-nanobot}"
export MOMO_AGENT_NANOBOT_SOURCE_DIR="${MOMO_AGENT_NANOBOT_SOURCE_DIR:-${NANOBOT_DIR}}"
export MOMO_AGENT_NANOBOT_TOOL_MODE="${MOMO_AGENT_NANOBOT_TOOL_MODE:-all}"
export MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS="${MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS:-0}"
export MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS="${MOMO_AGENT_NANOBOT_MAX_TOOL_ITERATIONS:-24}"
export PYTHONPATH="${REPO_ROOT}/Software/Master:${REPO_ROOT}/Software/Master/quick_control_api/src${PYTHONPATH:+:${PYTHONPATH}}"

API_BASE="${MOMO_AGENT_NANOBOT_API_BASE:-}"
if [[ -n "${API_BASE}" ]]; then
  API_HOST="$("${VENV_PYTHON}" - <<'PY'
from urllib.parse import urlparse
import os

raw = os.getenv("MOMO_AGENT_NANOBOT_API_BASE", "").strip()
host = urlparse(raw).hostname or ""
print(host)
PY
)"
  if [[ -n "${API_HOST}" ]]; then
    export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${API_HOST},127.0.0.1,localhost"
    export no_proxy="${no_proxy:+${no_proxy},}${API_HOST},127.0.0.1,localhost"
  fi
fi

exec "${VENV_PYTHON}" "${REPO_ROOT}/Software/Master/momo_agent/main.py" "$@"
