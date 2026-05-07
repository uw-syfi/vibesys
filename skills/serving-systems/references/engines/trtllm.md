# TensorRT-LLM source-code lookup

Short reference into `repos/TensorRT-LLM/`. Two runtimes — know which you're targeting before navigating.

## Setup

```bash
export SERVE_REPOS=<vibe-serve-root>/skills/serving-systems/repos
```

If `$SERVE_REPOS/TensorRT-LLM/` is missing (e.g. running inside a fresh agent sandbox where the submodule isn't mounted), fetch only the pinned commit this skill was authored against — the paths and line numbers in the tables below assume it:

```bash
mkdir -p "$SERVE_REPOS/TensorRT-LLM" && cd "$SERVE_REPOS/TensorRT-LLM"
git init -q
git remote add origin https://github.com/NVIDIA/TensorRT-LLM.git
git fetch --depth 1 origin 0d2bea7c3c99b734a8e09c4c767820e03136a15b
git checkout -q FETCH_HEAD
```

(From the vibe-serve repo root the equivalent is `git submodule update --init skills/serving-systems/repos/TensorRT-LLM`.)

## Runtimes

| Runtime | Location | When to use |
|:--------|:---------|:------------|
| **PyTorch** | `tensorrt_llm/_torch/` | default for new work; most new features land here |
| **C++** (legacy TRT engine) | `cpp/tensorrt_llm/` | max-perf deployments; Triton Inference Server backend |

## Directory map

```
TensorRT-LLM/
├── tensorrt_llm/                        # Python package
│   ├── llmapi/
│   │   └── llm.py                       # LLM class (default entrypoint → PyTorch runtime)
│   ├── executor/
│   │   └── executor.py                  # GenerationExecutor (Python side)
│   ├── _torch/                          # PyTorch runtime
│   │   ├── models/                      # modeling_llama.py, modeling_deepseekv3.py, modeling_qwen*.py, ...
│   │   ├── modules/
│   │   │   └── attention.py             # attention module wiring
│   │   ├── attention_backend/           # backends: trtllm, flashinfer, flashattention
│   │   ├── speculative/                 # drafters, eagle3, mtp, ngram, pard
│   │   ├── disaggregation/              # base, native, nixl (transceiver.py)
│   │   ├── pyexecutor/py_executor.py    # PyExecutor main loop
│   │   ├── auto_deploy/                 # torch.export-based AutoDeploy beta
│   │   └── shim/ad_executor.py
│   ├── quantization/                    # mode.py, functional.py, layers.py, fp8_quantize.py, fp4_utils.py
│   └── builder.py                       # legacy TRT engine build flow
├── cpp/
│   └── tensorrt_llm/
│       ├── executor/                    # executor.cpp + executorImpl.cpp (C++ dispatch)
│       ├── runtime/                     # bufferManager, gptDecoder, decoderState, ...
│       ├── kernels/                     # CUDA kernels (~26 subdirs)
│       └── batch_manager/               # trtGptModelInflightBatching.cpp (main dispatch loop)
├── triton_backend/
│   ├── inflight_batcher_llm/            # Triton Inference Server backend
│   └── all_models/                      # per-model Triton deployments
├── triton_kernels/                      # Python Triton kernels (matmul_ogs.py, distributed.py, ...)
├── examples/
│   └── models/core/                     # per-model examples: llama, qwen, deepseek, ...
└── benchmarks/
    └── cpp/                             # C++ benchmark harness
```

## Where's X?

| Need | Path (under `$SERVE_REPOS/TensorRT-LLM/`) |
|:-----|:-----------------------------------------|
| LLM API entrypoint | `tensorrt_llm/llmapi/llm.py` |
| Python executor | `tensorrt_llm/executor/executor.py` |
| PyExecutor main loop | `tensorrt_llm/_torch/pyexecutor/py_executor.py` |
| PyTorch runtime models | `tensorrt_llm/_torch/models/` |
| PyTorch attention wiring | `tensorrt_llm/_torch/modules/attention.py`, `tensorrt_llm/_torch/attention_backend/` |
| PyTorch speculative decoding | `tensorrt_llm/_torch/speculative/` (Eagle3, MTP, ngram, pard) |
| Disaggregated serving | `tensorrt_llm/_torch/disaggregation/` (base, native, nixl) |
| Quantization | `tensorrt_llm/quantization/{mode,functional,layers,fp8_quantize,fp4_utils}.py` |
| C++ executor dispatch | `cpp/tensorrt_llm/executor/{executor,executorImpl}.cpp` |
| C++ runtime core | `cpp/tensorrt_llm/runtime/` |
| C++ kernels | `cpp/tensorrt_llm/kernels/` |
| C++ batch manager (inflight) | `cpp/tensorrt_llm/batch_manager/trtGptModelInflightBatching.cpp` |
| Triton Inference Server backend | `triton_backend/inflight_batcher_llm/`, `triton_backend/all_models/` |
| Python Triton kernels | `triton_kernels/` |
| Legacy builder (TRT engine) | `tensorrt_llm/builder.py` |
| AutoDeploy (torch.export path) | `tensorrt_llm/_torch/auto_deploy/` |
| Per-model examples | `examples/models/core/{llama,qwen,deepseek,...}/` |
| Benchmarks | `benchmarks/cpp/` |

## Grep anchors

LLM entrypoint and default runtime:
```bash
rg "class LLM|class TorchLlmArgs|default.*runtime" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/llmapi/
```

PyExecutor loop:
```bash
rg "class PyExecutor|def _executor_loop|def step|def forward" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/pyexecutor/py_executor.py | head
```

Attention wiring in _torch models:
```bash
rg "self\.attn_backend|attention_backend|FlashInfer|TrtllmAttn" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/models/ -A 2
```

Speculative drafters:
```bash
rg "class.*Drafter|class Eagle3|class MTP|def propose" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/speculative/
```

Disaggregation transceiver:
```bash
rg "class Transceiver|send_cache|recv_cache" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/disaggregation/
```

FP8 / FP4 quant mode gate:
```bash
rg "QuantMode\.(FP8|FP4)|is_fp8|is_fp4|NVFP4|MXFP4" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/quantization/
```

C++ batch manager dispatch:
```bash
rg "forwardInflight|stepBatch|class TrtGptModelInflightBatching" \
   $SERVE_REPOS/TensorRT-LLM/cpp/tensorrt_llm/batch_manager/
```

Model implementations present (survey):
```bash
rg "^class.*ForCausalLM|^class.*Llama|^class.*DeepSeek|^class.*Qwen" \
   $SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/models/ -l
```

Triton Inference Server backend:
```bash
rg "TRITONBACKEND_|class.*Backend" \
   $SERVE_REPOS/TensorRT-LLM/triton_backend/inflight_batcher_llm/ | head
```

## See also

- `engines/vllm/`, `engines/sglang/` — contrast runtime designs
- `algorithms/async-scheduling/` — TRT-LLM sidesteps Python scheduler overhead by running the batch manager in C++ (`cpp/tensorrt_llm/batch_manager/`); covered there as an alternative to async Python scheduling
- `algorithms/*`
- `hardware/nvidia/` — TRT-LLM tracks the latest NVIDIA ISA (Hopper sm_90a, Blackwell sm_100a) fastest
