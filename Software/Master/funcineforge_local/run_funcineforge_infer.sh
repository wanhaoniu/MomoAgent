#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${FUNCINEFORGE_REPO_DIR:-${THIS_DIR}/vendor/FunCineForge}"
DEFAULT_ENV_NAME="funcineforge"
DEFAULT_OUTPUT_DIR="${FUNCINEFORGE_OUTPUT_DIR:-${THIS_DIR}/outputs}"

detect_conda_bin() {
    if [[ -n "${CONDA_BIN:-}" && -x "${CONDA_BIN}" ]]; then
        printf '%s\n' "${CONDA_BIN}"
        return 0
    fi
    if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE}" ]]; then
        printf '%s\n' "${CONDA_EXE}"
        return 0
    fi
    if command -v conda >/dev/null 2>&1; then
        command -v conda
        return 0
    fi
    for candidate in \
        "${HOME}/miniconda3/bin/conda" \
        "${HOME}/anaconda3/bin/conda" \
        "/opt/conda/bin/conda"; do
        if [[ -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

detect_env_name() {
    if [[ -n "${FUNCINEFORGE_ENV_NAME:-}" ]]; then
        printf '%s\n' "${FUNCINEFORGE_ENV_NAME}"
        return 0
    fi
    if [[ -n "${CONDA_DEFAULT_ENV:-}" && "${CONDA_DEFAULT_ENV}" != "base" ]]; then
        printf '%s\n' "${CONDA_DEFAULT_ENV}"
        return 0
    fi
    printf '%s\n' "${DEFAULT_ENV_NAME}"
}

CONDA_BIN="$(detect_conda_bin || true)"
if [[ -z "${CONDA_BIN}" || ! -x "${CONDA_BIN}" ]]; then
    echo "[Fun-CineForge] Conda executable not found." >&2
    exit 1
fi
ENV_NAME="$(detect_env_name)"

if [[ ! -d "${REPO_DIR}" ]]; then
    echo "[Fun-CineForge] Repo directory missing: ${REPO_DIR}" >&2
    echo "[Fun-CineForge] Run bootstrap_funcineforge.sh first." >&2
    exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1 && [[ ! -c /dev/nvidia0 ]] && [[ ! -e /dev/kfd ]]; then
    echo "[Fun-CineForge] No usable GPU device detected inside the OS." >&2
    exit 2
fi

if ! "${CONDA_BIN}" run -n "${ENV_NAME}" python -c "import hydra" >/dev/null 2>&1; then
    echo "[Fun-CineForge] Missing Python package: hydra-core" >&2
    echo "[Fun-CineForge] Re-run bootstrap_funcineforge.sh or install manually:" >&2
    echo "  ${CONDA_BIN} run -n ${ENV_NAME} python -m pip install hydra-core" >&2
    exit 3
fi

mkdir -p "${DEFAULT_OUTPUT_DIR}"

if [[ $# -eq 0 ]]; then
    set -- --output_dir "${DEFAULT_OUTPUT_DIR}"
fi

echo "[Fun-CineForge] Repo: ${REPO_DIR}"
echo "[Fun-CineForge] Conda bin: ${CONDA_BIN}"
echo "[Fun-CineForge] Conda env: ${ENV_NAME}"
echo "[Fun-CineForge] Extra args: $*"

cd "${REPO_DIR}/exps"
exec "${CONDA_BIN}" run -n "${ENV_NAME}" bash infer.sh "$@"
