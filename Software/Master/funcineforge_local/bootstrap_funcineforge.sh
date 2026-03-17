#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${FUNCINEFORGE_REPO_DIR:-${THIS_DIR}/vendor/FunCineForge}"
DEFAULT_ENV_NAME="funcineforge"
UPSTREAM_REPO_URL="${FUNCINEFORGE_REPO_URL:-https://github.com/FunAudioLLM/FunCineForge.git}"

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[Fun-CineForge] Missing command: $1" >&2
        exit 1
    fi
}

detect_ffmpeg_bin() {
    if [[ -n "${FFMPEG_BIN:-}" && -x "${FFMPEG_BIN}" ]]; then
        printf '%s\n' "${FFMPEG_BIN}"
        return 0
    fi
    if command -v ffmpeg >/dev/null 2>&1; then
        command -v ffmpeg
        return 0
    fi
    if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/ffmpeg" ]]; then
        printf '%s\n' "${CONDA_PREFIX}/bin/ffmpeg"
        return 0
    fi
    return 1
}

warn_ffmpeg() {
    local ffmpeg_bin
    ffmpeg_bin="$(detect_ffmpeg_bin || true)"
    if [[ -n "${ffmpeg_bin}" ]]; then
        echo "[Fun-CineForge] ffmpeg: ${ffmpeg_bin}"
        return 0
    fi
    echo "[Fun-CineForge] Warning: ffmpeg executable not found." >&2
    echo "[Fun-CineForge] Note: \`pip install ffmpeg\` only installs a Python package," >&2
    echo "[Fun-CineForge] not the \`ffmpeg\` command-line binary." >&2
    echo "[Fun-CineForge] Install a real binary, for example:" >&2
    echo "  conda install -n ${ENV_NAME:-funcineforge} -c conda-forge ffmpeg" >&2
    echo "[Fun-CineForge] Bootstrap will continue, but later steps may fail without ffmpeg." >&2
}

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

warn_gpu() {
    if command -v nvidia-smi >/dev/null 2>&1; then
        return 0
    fi
    if [[ -c /dev/nvidia0 ]] || [[ -f /proc/driver/nvidia/version ]]; then
        return 0
    fi
    if [[ -e /dev/kfd ]]; then
        return 0
    fi
    echo "[Fun-CineForge] Warning: no NVIDIA GPU is currently visible in this OS." >&2
    echo "[Fun-CineForge] Bootstrap can still continue, but inference will fail until a usable GPU stack is available." >&2
}

warn_hf_login() {
    if command -v hf >/dev/null 2>&1; then
        hf auth whoami >/dev/null 2>&1 && return 0
    fi
    if command -v huggingface-cli >/dev/null 2>&1; then
        huggingface-cli whoami >/dev/null 2>&1 && return 0
    fi
    echo "[Fun-CineForge] Warning: Hugging Face login not detected." >&2
    echo "[Fun-CineForge] Upstream setup.py tries ModelScope first, then Hugging Face." >&2
    echo "[Fun-CineForge] If ModelScope download fails, run: hf auth login" >&2
    echo "[Fun-CineForge] and accept: https://huggingface.co/FunAudioLLM/Fun-CineForge" >&2
}

need_cmd git

CONDA_BIN="$(detect_conda_bin || true)"
if [[ -z "${CONDA_BIN}" || ! -x "${CONDA_BIN}" ]]; then
    echo "[Fun-CineForge] Conda executable not found." >&2
    exit 1
fi
CONDA_BASE="$("${CONDA_BIN}" info --base)"
CONDA_SH="${CONDA_BASE}/etc/profile.d/conda.sh"
if [[ ! -f "${CONDA_SH}" ]]; then
    echo "[Fun-CineForge] conda.sh not found: ${CONDA_SH}" >&2
    exit 1
fi
ENV_NAME="$(detect_env_name)"

warn_gpu
warn_hf_login
warn_ffmpeg

mkdir -p "$(dirname "${REPO_DIR}")"
if [[ ! -d "${REPO_DIR}/.git" ]]; then
    echo "[Fun-CineForge] Cloning upstream repo to ${REPO_DIR}"
    git clone --depth 1 "${UPSTREAM_REPO_URL}" "${REPO_DIR}"
else
    echo "[Fun-CineForge] Reusing existing repo at ${REPO_DIR}"
fi

if ! "${CONDA_BIN}" env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
    echo "[Fun-CineForge] Creating conda env ${ENV_NAME}"
    "${CONDA_BIN}" create -y -n "${ENV_NAME}" python=3.10
else
    echo "[Fun-CineForge] Reusing conda env ${ENV_NAME}"
fi

echo "[Fun-CineForge] Conda bin: ${CONDA_BIN}"
echo "[Fun-CineForge] Target env: ${ENV_NAME}"

echo "[Fun-CineForge] Upgrading pip/setuptools/wheel"
"${CONDA_BIN}" run -n "${ENV_NAME}" python -m pip install --upgrade pip setuptools wheel

echo "[Fun-CineForge] Running upstream setup.py"
(
    # shellcheck disable=SC1091
    source "${CONDA_SH}"
    conda activate "${ENV_NAME}"
    cd "${REPO_DIR}"
    printf 'y\n' | python setup.py
)

echo "[Fun-CineForge] Installing missing runtime extras"
"${CONDA_BIN}" run -n "${ENV_NAME}" python -m pip install hydra-core

echo
echo "[Fun-CineForge] Bootstrap complete."
echo "[Fun-CineForge] Next step:"
echo "  bash ${THIS_DIR}/run_funcineforge_infer.sh"
