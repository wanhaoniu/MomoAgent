#!/usr/bin/env bash
set -euo pipefail

echo "[Fun-CineForge] GPU visibility check"
echo

echo "[lspci]"
lspci | grep -Ei 'vga|3d|display|nvidia|amd|intel' || true
echo

echo "[/dev]"
ls /dev | grep -E 'nvidia|dri|kfd' || true
echo

echo "[/proc/driver/nvidia/version]"
if [[ -f /proc/driver/nvidia/version ]]; then
    cat /proc/driver/nvidia/version
else
    echo "missing"
fi
echo

echo "[rocm-smi]"
if command -v rocm-smi >/dev/null 2>&1; then
    rocm-smi || true
else
    echo "rocm-smi not found"
fi
echo

echo "[nvidia-smi]"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi
else
    echo "nvidia-smi not found"
fi

echo
echo "[python torch gpu status]"
if command -v python >/dev/null 2>&1; then
    python - <<'PY' || true
try:
    import torch
    print("torch.__version__ =", torch.__version__)
    print("torch.cuda.is_available() =", torch.cuda.is_available())
    print("torch.cuda.device_count() =", torch.cuda.device_count())
    print("torch.version.cuda =", getattr(torch.version, "cuda", None))
    print("torch.version.hip =", getattr(torch.version, "hip", None))
    if torch.cuda.is_available():
        for idx in range(torch.cuda.device_count()):
            print(f"device[{idx}] =", torch.cuda.get_device_name(idx))
except Exception as exc:
    print("torch check failed:", exc)
PY
else
    echo "python not found"
fi
