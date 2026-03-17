## Fun-CineForge Local Deploy

This directory contains local helper scripts for deploying the official
`FunAudioLLM/Fun-CineForge` dubbing model.

Important:

- Upstream Fun-CineForge is a batch inference project.
- It does **not** expose an OpenAI-compatible HTTP API out of the box.
- If you want your current HMI to call it over HTTP, you will still need a thin
  service wrapper after the model runs successfully.

### Current blocker on this VM

This virtual machine does **not** currently expose a confirmed upstream-tested
GPU stack to the guest OS.

Observed locally:

- `lspci` only shows `VMware SVGA II Adapter`
- `/proc/driver/nvidia/version` is missing
- `/dev/nvidia*` nodes are missing

`Fun-CineForge` is not a small CPU-friendly model. The upstream project uses
`torchrun` and assumes GPU inference. CUDA/NVIDIA is the safest path because
that is what the upstream code path is clearly written for. AMD/ROCm may be
attempted if the machine exposes `/dev/kfd` and the installed PyTorch build is
ROCm-capable, but that path is not verified here.

### Upstream assumptions

From the upstream `README.md` and `exps/infer.sh`:

- Python `3.10`
- PyTorch `>= 2.1`
- `ffmpeg`
- Conda environment
- Official setup via `python setup.py`
- Inference via `bash exps/infer.sh`
- Local checkpoint layout under:
  - `funcineforge_zh_en/llm/...`
  - `funcineforge_zh_en/flow/...`
  - `funcineforge_zh_en/vocoder/...`

The upstream `setup.py` tries `ModelScope` first and falls back to `Hugging Face`.

If the ModelScope download path fails, then you need to:

1. Open `https://huggingface.co/FunAudioLLM/Fun-CineForge`
2. Accept the gated access terms
3. Login locally with `hf auth login`

### Files

- `check_gpu_visibility.sh`: quick guest-OS GPU visibility check
- `bootstrap_funcineforge.sh`: clone upstream repo, choose a conda env, run upstream setup
- `run_funcineforge_infer.sh`: run the official `exps/infer.sh` through `conda run`

### Recommended order

1. Run `bash bootstrap_funcineforge.sh`
2. If model download falls back to Hugging Face, complete HF login and gated access
3. Fix GPU passthrough until `bash check_gpu_visibility.sh` shows a usable GPU stack
4. Run `bash run_funcineforge_infer.sh`

If `bootstrap_funcineforge.sh` warns that `ffmpeg` is missing, install the real
binary, not the Python package:

```bash
conda install -n qwen -c conda-forge ffmpeg
```

`pip install ffmpeg` is not enough, because that does not provide the `ffmpeg`
CLI executable.

### Default paths

- Upstream repo clone:
  - `Software/Master/funcineforge_local/vendor/FunCineForge`
- Conda env:
  - active env if not `base`, otherwise `funcineforge`
- Default output dir:
  - `Software/Master/funcineforge_local/outputs`
