# The seed candidate image: what engine/ needs to serve Qwen3.5-9B on an MI210. The task
# (.vibesys/tasks/agentx/vibesys.input.toml, [candidate]) builds it from the candidate's
# commit with engine/ as the context and runs `python -m engine.server` in it from the
# checkout root, so the engine's code comes from the checkout and this image holds only its
# dependencies. It is the agent's to change: another base, a kernel library or a profiler is
# a legitimate part of a candidate.
#
# The base is AMD's ROCm 6.4 PyTorch image (ROCm userspace, rocprofv3 and the HIP runtime),
# pinned by digest. requirements.txt pins every Python distribution with hashes, including
# torch 2.9.1+rocm6.4 (the build the task's golden data was made with), which replaces the
# base's torch. Regenerate it after a change to the example's pyproject.toml:
#   uv pip compile <the pyproject's dependencies, torch==2.9.1+rocm6.4> --python-version 3.12 \
#     --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header --no-annotate \
#     --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/rocm6.4 \
#     --index-strategy unsafe-best-match -o engine/requirements.txt
# Model weights are never baked in: the operator mounts the Hugging Face cache.
FROM rocm/pytorch:rocm6.4.4_ubuntu24.04_py3.12_pytorch_release_2.7.1@sha256:f444357218627704168ef31d991cb6827999208d38906b9c22383c6a829f54e9

COPY requirements.txt /tmp/engine-requirements.txt
RUN python3 -m pip install --no-cache-dir --require-hashes --only-binary=:all: \
      --extra-index-url https://download.pytorch.org/whl/rocm6.4 \
      -r /tmp/engine-requirements.txt \
 && rm /tmp/engine-requirements.txt \
 && python3 -c "import torch; assert torch.version.hip, 'torch is not a ROCm build'; print('torch', torch.__version__, 'hip', torch.version.hip)"

ENV PYTHONUNBUFFERED=1
# The operator's Hugging Face cache, mounted read-only as "hf" (the task's [environment] needs).
ENV HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1
