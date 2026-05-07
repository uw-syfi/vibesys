# vLLM source-code lookup

Short reference pointing into `repos/vllm/` for common vLLM development tasks. The v0 engine is legacy; everything here refers to **v1** (the current default) unless stated otherwise.

## Setup

```bash
export SERVE_REPOS=<vibe-serve-root>/skills/serving-systems/repos
# or substitute $SERVE_REPOS inline in commands below.
```

If `$SERVE_REPOS/vllm/` is missing (e.g. running inside a fresh agent sandbox where the submodule isn't mounted), fetch only the pinned commit this skill was authored against — the paths and line numbers in the tables below assume it:

```bash
mkdir -p "$SERVE_REPOS/vllm" && cd "$SERVE_REPOS/vllm"
git init -q
git remote add origin https://github.com/vllm-project/vllm.git
git fetch --depth 1 origin 0210024ae796446a121f96d2d31053668ac0fd85
git checkout -q FETCH_HEAD
```

(From the vibe-serve repo root the equivalent is `git submodule update --init skills/serving-systems/repos/vllm`.)

## Directory map

```
vllm/                              # python package
├── v1/
│   ├── engine/
│   │   ├── llm_engine.py          # sync LLMEngine
│   │   └── async_llm.py           # AsyncLLMEngine
│   ├── core/
│   │   ├── sched/
│   │   │   ├── scheduler.py       # main scheduling logic
│   │   │   └── interface.py
│   │   ├── kv_cache_coordinator.py
│   │   └── kv_cache_manager.py
│   ├── attention/
│   │   ├── backend.py             # AttentionBackend ABC
│   │   └── backends/
│   │       ├── registry.py        # AttentionBackendEnum + register_backend
│   │       ├── flash_attn.py
│   │       ├── flashinfer.py
│   │       ├── triton_attn.py
│   │       ├── flex_attention.py
│   │       ├── mamba_attn.py
│   │       ├── rocm_attn.py
│   │       ├── linear_attn.py
│   │       └── cpu_attn.py
│   ├── worker/
│   │   ├── gpu_worker.py          # launch entrypoint
│   │   └── gpu/gpu_model_runner.py
│   ├── executor/
│   │   ├── abstract.py
│   │   ├── uniproc_executor.py
│   │   ├── multiproc_executor.py
│   │   └── ray_executor.py
│   ├── sample/sampler.py
│   ├── spec_decode/               # eagle.py, medusa.py, draft_model.py, suffix_decoding.py
│   └── structured_output/         # backend_outlines.py, backend_xgrammar.py, request.py
├── model_executor/
│   ├── models/                    # per-model files (llama.py, qwen2.py, ...) + registry.py
│   └── layers/quantization/       # awq.py, gptq.py, fp8.py, ... + base_config.py
├── compilation/                   # backends.py, cuda_graph.py, compiler_interface.py
├── distributed/                   # parallel_state.py, communication_op.py, device_communicators/
├── lora/                          # lora_model.py, model_manager.py, request.py
└── entrypoints/openai/api_server.py
```

Custom C++ / CUDA ops live at the repo root in `csrc/` (attention, quantization, cache kernels, …) — not under `vllm/`.

## Where's X?

| Need | Path (under `$SERVE_REPOS/vllm/`) |
|:-----|:----------------------------------|
| v1 sync / async engine entry | `vllm/v1/engine/llm_engine.py`, `vllm/v1/engine/async_llm.py` |
| Scheduler (v1) | `vllm/v1/core/sched/scheduler.py` |
| KV cache coordinator / manager | `vllm/v1/core/kv_cache_coordinator.py`, `vllm/v1/core/kv_cache_manager.py` |
| Attention backend base class | `vllm/v1/attention/backend.py` |
| Attention backend registry | `vllm/v1/attention/backends/registry.py` |
| Individual attention backends | `vllm/v1/attention/backends/{flash_attn,flashinfer,triton_attn,flex_attention,mamba_attn,rocm_attn,linear_attn,cpu_attn}.py` |
| Model implementations | `vllm/model_executor/models/` (+ `registry.py`) |
| Quantization schemes | `vllm/model_executor/layers/quantization/` |
| Speculative decoding (v1) | `vllm/v1/spec_decode/` |
| Structured output (v1) | `vllm/v1/structured_output/` |
| Executor (multiproc / ray / uniproc) | `vllm/v1/executor/` |
| GPU worker / model runner | `vllm/v1/worker/gpu_worker.py`, `vllm/v1/worker/gpu/gpu_model_runner.py` |
| Sampler (v1) | `vllm/v1/sample/sampler.py` |
| OpenAI API server | `vllm/entrypoints/openai/api_server.py` |
| Compilation / torch.compile / CUDA graph | `vllm/compilation/` |
| Distributed / parallelism | `vllm/distributed/` |
| LoRA | `vllm/lora/` |
| Custom CUDA / C++ ops | `csrc/` (repo root, not under `vllm/`) |

## Grep anchors

Attention backend selection and dispatch:
```bash
rg "AttentionBackendEnum|register_backend|get_attention_backend" \
   $SERVE_REPOS/vllm/vllm/v1/attention --type py
```

Scheduler batch-selection logic:
```bash
rg "def schedule\(|def _schedule|add_seq" \
   $SERVE_REPOS/vllm/vllm/v1/core/sched/scheduler.py
```

Model class wiring (e.g., Llama):
```bash
rg "class LlamaModel|class LlamaForCausalLM|@register_model" \
   $SERVE_REPOS/vllm/vllm/model_executor/models -A 3
```

Quantization dispatch / loading:
```bash
rg "QuantizationConfig|get_quant_method|apply\b" \
   $SERVE_REPOS/vllm/vllm/model_executor/layers/quantization
```

Sampler token selection:
```bash
rg "class Sampler|TopKTopPSampler|sample_from" \
   $SERVE_REPOS/vllm/vllm/v1/sample
```

Speculative decoding propose / verify:
```bash
rg "propose_tokens|verify_tokens|class.*Drafter|Eagle3Head" \
   $SERVE_REPOS/vllm/vllm/v1/spec_decode
```

GPU worker execute / model-runner forward:
```bash
rg "class GPUWorker|def execute_model|class GPUModelRunner" \
   $SERVE_REPOS/vllm/vllm/v1/worker
```

Ray executor task dispatch:
```bash
rg "class RayExecutor|execute_model_async|collective_rpc" \
   $SERVE_REPOS/vllm/vllm/v1/executor/ray_executor.py
```

CUDA graph capture:
```bash
rg "class CUDAGraphRunner|capture\(" \
   $SERVE_REPOS/vllm/vllm/compilation/cuda_graph.py
```

Custom CUDA op entry:
```bash
rg "TORCH_LIBRARY|PYBIND11_MODULE" $SERVE_REPOS/vllm/csrc/
```

## See also

- `engines/sglang/`, `engines/trtllm/` — contrast vLLM's design with the other two
- `algorithms/async-scheduling/` — vLLM's `AsyncScheduler` (at `vllm/v1/core/sched/async_scheduler.py` + `vllm/v1/worker/gpu/async_utils.py`) is the canonical example; stacks with CUDA graphs + batched sampling
- `algorithms/*` — concept behind each source location
- `backends/flashinfer/`, `backends/flashattention/` — how the attention backends call out
