# Multi-LoRA serving

One base model, many adapters, one process. Each incoming request names an adapter; the engine ensures the adapter is resident before running the step and routes the request's tokens through the correct low-rank deltas.

## Prerequisites

- The base model already serves correctly without LoRA.
- Each adapter is a LoRA checkpoint in **HuggingFace PEFT** or **NeMo** format, sized so that `max_lora_rank` covers the highest-rank adapter.
- You know which modules the adapters were trained on (`attn_q`, `attn_k`, `attn_v`, `attn_dense`, `mlp_h_to_4h`, `mlp_4h_to_h`, …). Missing a trained module silently gives wrong outputs.

## Why this is a serving problem, not a modeling problem

LoRA itself is simple — `W + α·B·A` — and merging it permanently is trivial. The *serving* problem is what happens when concurrent requests want different adapters:

- Merge-per-request: recomputes `W + B·A` every step, kills throughput.
- Materialize every adapter on GPU: memory blows up past a dozen adapters.
- Route per-request through the unmerged `B·A` path, keep a small adapter cache, swap from CPU on miss.

The third option is what all three production engines implement. The knobs are the **cache sizes** (`max_loras` on GPU, `max_cpu_loras` on host) and the **target-module list** (which linear layers get the deltas).

## Workflow (TRT-LLM shape; vLLM / SGLang are analogous)

```python
from tensorrt_llm import LLM
from tensorrt_llm.lora_manager import LoraConfig
from tensorrt_llm.executor.request import LoRARequest
from tensorrt_llm.llmapi.llm_args import PeftCacheConfig

lora_config = LoraConfig(
    lora_target_modules=["attn_q", "attn_k", "attn_v"],
    max_lora_rank=16,
    max_loras=4,          # adapters resident on GPU
    max_cpu_loras=32,     # adapters cached on host, swapped in on demand
)

peft_cache_config = PeftCacheConfig(
    host_cache_size=1 << 30,   # 1 GiB CPU pool
    device_cache_percent=0.1,  # 10% of free GPU memory
)

llm = LLM(
    model="/path/to/base",
    lora_config=lora_config,
    peft_cache_config=peft_cache_config,
)

outputs = llm.generate(
    prompts=[p1, p2, p3],
    lora_request=[                                  # one per prompt; None disables LoRA
        LoRARequest("translate", 0, "/loras/translate"),
        LoRARequest("summarize", 1, "/loras/summarize"),
        None,
    ],
)
```

Key invariants:

- `lora_int_id` is the cache key. Reusing it within a process implies "the same adapter". Diverging paths with the same int_id is a silent bug.
- The first request per adapter pays the load-from-disk cost; subsequent requests hit CPU cache; hot adapters stay resident on GPU.
- `max_lora_rank` is a *ceiling*, not a per-adapter rank. Leave headroom for higher-rank adapters you might add later — changing it requires re-initializing.

## OpenAI-compatible wire format

All three engines overload the OpenAI request envelope to name the adapter. TRT-LLM (`trtllm-serve`) and vLLM use an `extra_body` / sidecar field:

```python
client.completions.create(
    model="/path/to/base",
    prompt="...",
    extra_body={
        "lora_request": {
            "lora_name": "translate",
            "lora_int_id": 0,
            "lora_path": "/loras/translate",
        },
    },
)
```

SGLang's OpenAI adapter accepts `lora_path` directly on the request. Check the target engine's docs — the wire shape is the one thing that differs most across implementations.

## Compatibility

| Axis | Interaction |
|:-----|:------------|
| **Quantization** | LoRA composes with FP8 / FP4 base weights; the `B·A` product is computed in higher precision and added post-dequant. Quantized + LoRA is supported in all three engines. The adapter weights themselves are typically BF16. |
| **Tensor parallelism** | LoRA matrices shard the same way as the base layer's column / row parallel partitioning. An adapter trained under TP=1 must be resharded to load under TP>1. TRT-LLM handles this at load time; failing to reshard silently mismatches at the all-reduce. |
| **CUDA graphs** | Adapter dispatch is data-dependent (which rows of the LoRA tensor to read). Implementations stash a LoRA routing index tensor that is graph-safe — captured alongside block tables. |
| **Speculative decoding** | Draft model has its own (smaller) LoRA, or the draft runs without LoRA and the target runs with LoRA. Mixing requires care with the decode step's batch-dim routing. |
| **Disaggregated serving** | The adapter must be resident on *both* the prefill and decode engines; they do not share LoRA cache state over the KV transceiver. |

## Engine pointers

| Engine | LoRA code |
|:-------|:----------|
| TensorRT-LLM | `tensorrt_llm/lora_manager.py`, `tensorrt_llm/executor/request.py::LoRARequest`, `tensorrt_llm/llmapi/llm_args.py::PeftCacheConfig` |
| vLLM | `$SERVE_REPOS/vllm/vllm/lora/` (`lora_model.py`, `model_manager.py`, `punica_wrapper/`, `ops/`) |
| SGLang | `$SERVE_REPOS/sglang/python/sglang/srt/lora/` (`lora_manager.py`, `lora_overlap_loader.py`, `lora_registry.py`, `eviction_policy.py`) |

## Pitfalls

- **Target module mismatch.** The adapter was trained on `attn_q / attn_k / attn_v` but the config lists only `attn_q`. No error — the other two deltas are silently dropped and outputs are wrong.
- **Format sniffing.** HF PEFT (`adapter_model.safetensors` + `adapter_config.json`) vs. NeMo (`.nemo` tarball). TRT-LLM requires `lora_ckpt_source="nemo"` for the latter.
- **CPU cache thrash.** `max_cpu_loras` too small relative to the working set causes disk loads on the hot path. Size it for the 95th-percentile distinct-adapter-per-minute count.
- **Per-batch routing cost.** The LoRA `B·A` path is a GEMM with a gather; at small batch sizes it can dominate over the base matmul. Don't benchmark "LoRA overhead" at batch=1 — it over-reports.
- **Evaluation leaks.** A request that forgets to set `lora_request` silently runs on the base model. Accuracy regressions without perf regressions are usually this.
- **TP + adapter reshard.** Trained at TP=1, served at TP=4, loaded naively: OOM or shape mismatch. Engines reshard on load — verify your engine actually does it for your format.

## See also

- `algorithms/parallelism/` — how LoRA adapters must shard alongside TP
- `algorithms/quantization-schemes/` — FP8 / FP4 base + LoRA
- `engines/trtllm/`, `engines/vllm/`, `engines/sglang/` — LoRA implementation paths
- TRT-LLM reference: `$SERVE_REPOS/TensorRT-LLM/docs/source/features/lora.md`
