#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/bootstrap_nanobot.sh
  bash scripts/bootstrap_nanobot.sh --recreate
  bash scripts/bootstrap_nanobot.sh --python python3.11
  bash scripts/bootstrap_nanobot.sh --install-native-skill-deps
  bash scripts/bootstrap_nanobot.sh --skip-pull

Options:
  --recreate   Remove the existing .venv-nanobot and create it again.
  --install-native-skill-deps
               Install recommended Homebrew packages for nanobot builtin skills.
  --python     Use a specific Python executable or absolute path.
  --skip-pull  Do not fetch/pull the vendored nanobot clone.
  -h, --help   Show this help message.
EOF
}

RECREATE=0
INSTALL_NATIVE_SKILL_DEPS=0
SKIP_PULL=0
PYTHON_HINT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --recreate)
      RECREATE=1
      ;;
    --install-native-skill-deps)
      INSTALL_NATIVE_SKILL_DEPS=1
      ;;
    --python)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[nanobot-bootstrap] Missing value for --python" >&2
        exit 1
      fi
      PYTHON_HINT="$1"
      ;;
    --skip-pull)
      SKIP_PULL=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[nanobot-bootstrap] Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
NANOBOT_DIR="${REPO_ROOT}/external/nanobot"
VENV_DIR="${REPO_ROOT}/.venv-nanobot"
REQ_FILE="${REPO_ROOT}/requirements/nanobot-bridge.txt"
SDK_DIR="${REPO_ROOT}/sdk"

install_brew_formula_if_missing() {
  local formula="$1"
  local bin_name="$2"
  if command -v "${bin_name}" >/dev/null 2>&1; then
    echo "[nanobot-bootstrap] ${bin_name} already available"
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "[nanobot-bootstrap] Homebrew not found, skipping ${formula}" >&2
    return 0
  fi
  echo "[nanobot-bootstrap] Installing ${formula} via Homebrew"
  brew install "${formula}"
}

choose_python() {
  if [[ -n "${PYTHON_HINT}" ]]; then
    if [[ "${PYTHON_HINT}" == */* ]]; then
      [[ -x "${PYTHON_HINT}" ]] || return 1
      printf '%s\n' "${PYTHON_HINT}"
      return 0
    fi
    command -v "${PYTHON_HINT}" >/dev/null 2>&1 || return 1
    command -v "${PYTHON_HINT}"
    return 0
  fi

  local candidate
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(choose_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[nanobot-bootstrap] Could not find a usable Python interpreter." >&2
  echo "[nanobot-bootstrap] Install Python 3.11+ and rerun, or pass --python /path/to/python." >&2
  exit 1
fi

PYTHON_VERSION="$("${PYTHON_BIN}" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"

if ! "${PYTHON_BIN}" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "[nanobot-bootstrap] Python 3.11+ is required. Found ${PYTHON_VERSION} at ${PYTHON_BIN}." >&2
  exit 1
fi

if [[ ! -d "${NANOBOT_DIR}/.git" ]]; then
  echo "[nanobot-bootstrap] Cloning HKUDS/nanobot into ${NANOBOT_DIR}"
  mkdir -p "${REPO_ROOT}/external"
  git clone --depth 1 https://github.com/HKUDS/nanobot.git "${NANOBOT_DIR}"
elif [[ "${SKIP_PULL}" -eq 0 ]]; then
  echo "[nanobot-bootstrap] Updating vendored nanobot clone"
  git -C "${NANOBOT_DIR}" pull --ff-only
else
  echo "[nanobot-bootstrap] Reusing existing vendored nanobot clone"
fi

if [[ "${RECREATE}" -eq 1 && -d "${VENV_DIR}" ]]; then
  echo "[nanobot-bootstrap] Removing existing virtual environment at ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[nanobot-bootstrap] Creating virtual environment with ${PYTHON_BIN} (${PYTHON_VERSION})"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "[nanobot-bootstrap] Reusing existing virtual environment at ${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "[nanobot-bootstrap] Virtual environment looks incomplete: ${VENV_PYTHON} not found." >&2
  exit 1
fi

echo "[nanobot-bootstrap] Upgrading pip tooling"
"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel

echo "[nanobot-bootstrap] Installing lightweight bridge requirements"
"${VENV_PYTHON}" -m pip install -r "${REQ_FILE}"

echo "[nanobot-bootstrap] Installing local SDK in editable mode"
"${VENV_PYTHON}" -m pip install -e "${SDK_DIR}"

echo "[nanobot-bootstrap] Installing vendored nanobot in editable mode"
"${VENV_PYTHON}" -m pip install -e "${NANOBOT_DIR}"

if [[ "${INSTALL_NATIVE_SKILL_DEPS}" -eq 1 ]]; then
  install_brew_formula_if_missing "tmux" "tmux"
  install_brew_formula_if_missing "steipete/tap/summarize" "summarize"
fi

echo
echo "[nanobot-bootstrap] Setup complete."
echo "[nanobot-bootstrap] Next steps:"
echo "  source .venv-nanobot/bin/activate"
echo "  export MOMO_AGENT_BACKEND=nanobot"
echo "  export MOMO_AGENT_NANOBOT_SOURCE_DIR=${NANOBOT_DIR}"
echo "  export MOMO_AGENT_NANOBOT_API_BASE=http://172.18.29.16:1234/v1"
echo "  export MOMO_AGENT_NANOBOT_MODEL=qwen/qwen3.5-35b-a3b"
echo "  export MOMO_AGENT_NANOBOT_TOOL_MODE=all"
echo "  export MOMO_AGENT_NANOBOT_DISABLE_BUILTIN_SKILLS=0"
echo "  bash scripts/run_momo_nanobot.sh warmup"
