#!/bin/bash
# Launch the tuned vLLM comparison server for the Qwen3.5-9B / 1x MI210
# throughput objective inside an apptainer image. This is the external
# comparison point (see ../OBJECTIVE.md and ../README.md), not the candidate.
#
# Run on a node with the MI210 visible (e.g. inside a Slurm allocation):
#   VLLM_SIF=/path/to/vllm-openai-rocm.sif VLLM_CACHE_DIR=/path/to/cache \
#     ./vllm_baseline.sh <port> [extra vllm serve args...]
#
# Required env:
#   VLLM_SIF        apptainer image with a ROCm build of vLLM
#   VLLM_CACHE_DIR  writable directory for apptainer, HF, vLLM, Triton, and
#                   inductor caches; the model must already be in
#                   $VLLM_CACHE_DIR/hf (the server runs with HF_HUB_OFFLINE=1)
# Optional env (defaults are the tuned values):
#   MAX_MODEL_LEN, GPU_MEM_UTIL, MAX_NUM_SEQS, MAX_NUM_BATCHED_TOKENS,
#   ATTENTION_BACKEND, ASYNC_SCHEDULING (1/0), ENABLE_PREFIX_CACHING (1/0),
#   VLLM_EXTRA_BINDS (comma-separated apptainer --bind specs)
set -euo pipefail

PORT="${1:?usage: vllm_baseline.sh <port> [extra vllm serve args...]}"
shift || true

SIF="${VLLM_SIF:?set VLLM_SIF to the vLLM ROCm apptainer image (.sif)}"
CACHE="${VLLM_CACHE_DIR:?set VLLM_CACHE_DIR to a writable cache directory}"
if [ ! -f "$SIF" ]; then
  echo "error: VLLM_SIF=$SIF does not exist" >&2
  exit 1
fi
mkdir -p "$CACHE"

MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.95}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-256}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-16384}"
# TRITON_ATTN, not vLLM's ROCM_ATTN default: on this model/GPU ROCM_ATTN
# cannot use its custom paged-attention kernel for the chunked-prefill/decode
# path ("Cannot use ROCm custom paged attention kernel, falling back to Triton
# implementation") and falls back to Triton inside its own dispatch.
# Selecting TRITON_ATTN directly skips that path. Re-verify if a later vLLM
# build fixes the ROCm kernel on gfx90a.
ATTENTION_BACKEND="${ATTENTION_BACKEND:-TRITON_ATTN}"
ASYNC_SCHEDULING="${ASYNC_SCHEDULING:-1}"
ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"

export APPTAINER_CACHEDIR="$CACHE/apptainer_cache" APPTAINER_TMPDIR="$CACHE/apptainer_tmp"
export APPTAINERENV_HF_HOME="$CACHE/hf" APPTAINERENV_HF_HUB_OFFLINE=1 \
       APPTAINERENV_VLLM_CACHE_ROOT="$CACHE/vllm_cache" \
       APPTAINERENV_TRITON_CACHE_DIR="$CACHE/triton_cache" \
       APPTAINERENV_XDG_CACHE_HOME="$CACHE/xdg" \
       APPTAINERENV_TORCHINDUCTOR_CACHE_DIR="$CACHE/xdg/inductor"

ARGS=(
  serve Qwen/Qwen3.5-9B
  --dtype bfloat16
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_MEM_UTIL"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
  --attention-backend "$ATTENTION_BACKEND"
  --enable-prompt-tokens-details
  --port "$PORT"
)
if [ "$ENABLE_PREFIX_CACHING" = "1" ]; then
  ARGS+=(--enable-prefix-caching)
else
  ARGS+=(--no-enable-prefix-caching)
fi
if [ "$ASYNC_SCHEDULING" = "1" ]; then
  ARGS+=(--async-scheduling)
else
  ARGS+=(--no-async-scheduling)
fi

BINDS=(--bind "$CACHE:$CACHE")
if [ -n "${VLLM_EXTRA_BINDS:-}" ]; then
  BINDS+=(--bind "$VLLM_EXTRA_BINDS")
fi

echo "### launching vllm serve: ${ARGS[*]} $*" >&2
exec apptainer exec --rocm "${BINDS[@]}" "$SIF" vllm "${ARGS[@]}" "$@"
