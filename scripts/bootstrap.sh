#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/bootstrap.sh
  bash scripts/bootstrap.sh --advanced
  bash scripts/bootstrap.sh --recreate
  bash scripts/bootstrap.sh --python python3.10

Options:
  --advanced   Install the advanced GUI stack on top of the base environment.
  --recreate   Remove the existing .venv and create it again from scratch.
  --python     Use a specific Python executable or absolute path.
  -h, --help   Show this help message.
EOF
}

MODE="base"
RECREATE=0
PYTHON_HINT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --advanced)
      MODE="advanced"
      ;;
    --base)
      MODE="base"
      ;;
    --recreate)
      RECREATE=1
      ;;
    --python)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[bootstrap] Missing value for --python" >&2
        exit 1
      fi
      PYTHON_HINT="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[bootstrap] Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
REQ_FILE="${REPO_ROOT}/requirements/base.txt"
if [[ "${MODE}" == "advanced" ]]; then
  REQ_FILE="${REPO_ROOT}/requirements/advanced.txt"
fi

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
  for candidate in python3.10 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(choose_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[bootstrap] Could not find a usable Python interpreter." >&2
  echo "[bootstrap] Install Python 3.10+ and rerun, or pass --python /path/to/python." >&2
  exit 1
fi

PYTHON_VERSION="$("${PYTHON_BIN}" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"

if ! "${PYTHON_BIN}" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "[bootstrap] Python 3.10+ is required. Found ${PYTHON_VERSION} at ${PYTHON_BIN}." >&2
  exit 1
fi

if [[ "${RECREATE}" -eq 1 && -d "${VENV_DIR}" ]]; then
  echo "[bootstrap] Removing existing virtual environment at ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[bootstrap] Creating virtual environment with ${PYTHON_BIN} (${PYTHON_VERSION})"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "[bootstrap] Reusing existing virtual environment at ${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "[bootstrap] Virtual environment looks incomplete: ${VENV_PYTHON} not found." >&2
  exit 1
fi

echo "[bootstrap] Upgrading pip tooling"
"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel

echo "[bootstrap] Installing ${MODE} requirements from ${REQ_FILE}"
"${VENV_PYTHON}" -m pip install -r "${REQ_FILE}"

echo "[bootstrap] Installing local SDK in editable mode"
"${VENV_PYTHON}" -m pip install -e "${REPO_ROOT}/sdk"

echo
echo "[bootstrap] Setup complete."
echo "[bootstrap] Next steps:"
echo "  source .venv/bin/activate"
if [[ "${MODE}" == "advanced" ]]; then
  echo "  python Software/Master/main.py"
else
  echo "  python Software/Master/quick_control_api/main.py --host 0.0.0.0 --port 8010"
fi
