# SGLang source-code lookup

Short reference into `repos/sglang/`. Depends on the `skills/serving-systems/repos/sglang` submodule being initialized.

## Setup

```bash
export SERVE_REPOS=<vibe-serve-root>/skills/serving-systems/repos
# or substitute $SERVE_REPOS inline below.
```

If `$SERVE_REPOS/sglang/` is missing (e.g. running inside a fresh agent sandbox where the submodule isn't mounted), fetch only the pinned commit this skill was authored against — the paths and line numbers in the tables below assume it:

```bash
mkdir -p "$SERVE_REPOS/sglang" && cd "$SERVE_REPOS/sglang"
git init -q
git remote add origin https://github.com/sgl-project/sglang.git
git fetch --depth 1 origin 04b1caf75b3c6f043a979ddce21d43ed07c217a6
git checkout -q FETCH_HEAD
```

(From the vibe-serve repo root the equivalent is `git submodule update --init skills/serving-systems/repos/sglang`.)

## Directory map

```
sglang/
├── python/sglang/
│   ├── launch_server.py                          # top-level launcher
│   ├── srt/                                      # the serving runtime
│   │   ├── managers/
│   │   │   ├── scheduler.py                      # main scheduling loop
│   │   │   ├── tokenizer_manager.py
│   │   │   └── detokenizer_manager.py
│   │   ├── mem_cache/
│   │   │   ├── radix_cache.py
│   │   │   ├── hiradix_cache.py
│   │   │   └── hicache_storage.py
│   │   ├── layers/
│   │   │   ├── attention/
│   │   │   │   ├── base_attn_backend.py
│   │   │   │   ├── attention_registry.py
│   │   │   │   ├── flashinfer_backend.py
│   │   │   │   ├── flashinfer_mla_backend.py
│   │   │   │   ├── cutlass_mla_backend.py
│   │   │   │   ├── flashattention_backend.py
│   │   │   │   ├── flashmla_backend.py
│   │   │   │   ├── triton_backend.py
│   │   │   │   ├── nsa_backend.py
│   │   │   │   ├── tbo_backend.py
│   │   │   │   ├── wave_backend.py
│   │   │   │   └── aiter_backend.py
│   │   │   ├── moe/
│   │   │   │   ├── router.py
│   │   │   │   ├── token_dispatcher/
│   │   │   │   ├── moe_runner/
│   │   │   │   └── ep_moe/                       # expert parallel + EPLB
│   │   │   └── quantization/                     # base_scheme.py, configs/, compressed_tensors/
│   │   ├── models/                               # per-model files
│   │   │   ├── deepseek_v2.py
│   │   │   ├── deepseek_nextn.py
│   │   │   └── deepseek_common/                  # shared DeepSeek components
│   │   ├── speculative/                          # eagle_worker.py, base_spec_worker.py, eagle_utils.py
│   │   ├── disaggregation/                       # encode_server.py, decode.py, base/
│   │   ├── distributed/                          # parallel_state.py, communication_op.py, device_communicators/
│   │   ├── compilation/                          # compile.py, cuda_piecewise_backend.py, compiler_interface.py
│   │   ├── model_loader/                         # loader.py, weight_utils.py
│   │   ├── entrypoints/
│   │   │   ├── engine.py
│   │   │   └── openai/serving_chat.py
│   │   ├── lora/                                 # lora_manager.py, layers.py, backend/
│   │   └── hardware_backend/                     # cuda / rocm / mlx / musa / npu adapters
│   └── jit_kernel/                               # Python Triton + CuTeDSL kernels
└── sgl-kernel/
    ├── csrc/                                     # attention, moe, quantization, kvcacheio, mamba, gemm, ...
    ├── include/
    └── python/
```

## Where's X?

| Need | Path (under `$SERVE_REPOS/sglang/`) |
|:-----|:------------------------------------|
| Scheduler, TokenizerManager, DetokenizerManager | `python/sglang/srt/managers/{scheduler,tokenizer_manager,detokenizer_manager}.py` |
| Radix cache + HiCache | `python/sglang/srt/mem_cache/{radix_cache,hiradix_cache,hicache_storage}.py` |
| Attention backend base + registry | `python/sglang/srt/layers/attention/{base_attn_backend,attention_registry}.py` |
| Individual attention backends | `python/sglang/srt/layers/attention/*_backend.py` (see dir map) |
| MoE routing + dispatch | `python/sglang/srt/layers/moe/{router.py,token_dispatcher/,moe_runner/}` |
| EPLB (expert load balancing) | `python/sglang/srt/layers/moe/ep_moe/` |
| Quantization | `python/sglang/srt/layers/quantization/{base_scheme.py,configs/,compressed_tensors/}` |
| Model implementations | `python/sglang/srt/models/` |
| DeepSeek V2 / V3 | `python/sglang/srt/models/deepseek_v2.py`, `.../deepseek_common/` |
| Speculative decoding (EAGLE) | `python/sglang/srt/speculative/{eagle_worker,base_spec_worker,eagle_utils}.py` |
| Disaggregated serving | `python/sglang/srt/disaggregation/` |
| Distributed (TP/PP/EP) | `python/sglang/srt/distributed/{parallel_state,communication_op}.py` |
| CUDA graph + piecewise compile | `python/sglang/srt/compilation/{compile,cuda_piecewise_backend,compiler_interface}.py` |
| Model loader / weight mapping | `python/sglang/srt/model_loader/{loader,weight_utils}.py` |
| Engine + OpenAI server entrypoints | `python/sglang/srt/entrypoints/engine.py`, `.../openai/serving_chat.py` |
| Launcher | `python/sglang/launch_server.py` |
| LoRA | `python/sglang/srt/lora/{lora_manager.py,layers.py,backend/}` |
| Hardware backend adapters | `python/sglang/srt/hardware_backend/` |
| JIT Triton / CuTeDSL kernels | `python/sglang/jit_kernel/` |
| Custom CUDA kernels (sgl-kernel) | `sgl-kernel/csrc/` |

## Grep anchors

Attention backend base + registration:
```bash
rg "class AttentionBackend|register_attention_backend|ATTENTION_BACKENDS" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/attention
```

Scheduler batch selection:
```bash
rg "def get_next_batch_to_run|def _get_new_batch_prefill" \
   $SERVE_REPOS/sglang/python/sglang/srt/managers/scheduler.py
```

Radix cache:
```bash
rg "class RadixCache|match_prefix|insert" \
   $SERVE_REPOS/sglang/python/sglang/srt/mem_cache/radix_cache.py
```

DeepSeek MoE routing wiring:
```bash
rg "class MoEGate|class DeepseekV2MoE|def routed_experts" \
   $SERVE_REPOS/sglang/python/sglang/srt/models/deepseek_v2.py
```

Speculative decode verify / accept:
```bash
rg "def verify|class.*SpecWorker|acceptance" \
   $SERVE_REPOS/sglang/python/sglang/srt/speculative/eagle_worker.py
```

Engine / launcher entry:
```bash
rg "class Engine|def launch_engine|launch_server" \
   $SERVE_REPOS/sglang/python/sglang/srt/entrypoints/engine.py \
   $SERVE_REPOS/sglang/python/sglang/launch_server.py
```

MoE token dispatcher:
```bash
rg "class.*TokenDispatcher|def dispatch|def combine" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/moe/token_dispatcher/
```

Disaggregation encode / decode servers:
```bash
rg "class EncodeServer|class DecodeServer|transceiver|KVSender|KVReceiver" \
   $SERVE_REPOS/sglang/python/sglang/srt/disaggregation/
```

Quantization method dispatch:
```bash
rg "class.*QuantScheme|get_quant_method|apply_weights" \
   $SERVE_REPOS/sglang/python/sglang/srt/layers/quantization/
```

## See also

- `engines/vllm/`, `engines/trtllm/`
- `algorithms/async-scheduling/` — SGLang's overlap scheduler (`event_loop_overlap`, `FutureMap`, `forward_stream` / `schedule_stream`) is the canonical "zero-overhead" implementation; the skill walks through the code
- `algorithms/*` — concepts behind each source location
- `backends/flashinfer/` — used by FlashInfer + FlashInfer-MLA backends here
