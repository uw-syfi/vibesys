# Quantization schemes

Not kernels — *schemes*. Which precision, which granularity, which calibration, which checkpoint layout. Kernel implementation defers to backend libraries and `agent-gpu-skills`.

## Axes

```
precision  ×  granularity  ×  what is quantized  ×  calibration
```

| Axis | Common values |
|:-----|:--------------|
| Precision | FP8 (E4M3, E5M2), INT8, INT4, FP4 (MXFP4, NVFP4), mixed |
| Granularity | per-tensor, per-channel (row/col), per-block (e.g., 128×128), per-group (e.g., group_size=128), per-token |
| What | weights only (W), weights + activations (W+A), KV cache, MoE experts only |
| Calibration | static (calibrated constants), dynamic (per-batch), none (e.g., symmetric INT4 weight-only) |

## Scheme families

### FP8

| Scheme | Granularity | Where it shines |
|:-------|:------------|:----------------|
| FP8 E4M3 per-tensor | single scale | simplest, highest throughput, some accuracy loss |
| FP8 per-channel (weights) | col-scales | common default for W+A FP8 |
| **FP8 block (DeepSeek)** | 1×128 activations, 128×128 weights | DeepSeek-V3 native format; needs DeepGEMM |
| FP8 KV cache | per-tensor / per-head | halves KV memory; small accuracy hit |

E4M3 is the standard for weights/activations; E5M2 sees more use for gradients / training.

### INT4 weight-only

| Scheme | Layout | Notes |
|:-------|:-------|:------|
| **AWQ** | group-wise (group_size=128), activation-aware clipping | HF standard, Marlin kernel |
| **GPTQ** | group-wise, OBS-based calibration | older, still prevalent |
| **Marlin** | GPTQ/AWQ data, rearranged for Hopper/Ampere | the *kernel*, consumes AWQ/GPTQ checkpoints |
| **GGUF** | various (Q4_K_M, Q5_K_S, ...) | llama.cpp family |
| **Petit** | weight-only INT4 | SGLang |

### FP4

| Scheme | Origin | Hardware |
|:-------|:-------|:---------|
| **MXFP4** | OCP microscaling (block=32, shared FP8 scale) | portable across vendors |
| **NVFP4** | Blackwell-native 4-bit format | Blackwell sm_100+ |

FP4 typically needs finer granularity (e.g., 16 or 32 per block) and post-training quantization with careful calibration.

### KV cache quant

Orthogonal to weight/activation quant:
- **FP8 KV**: E4M3 or E5M2, per-tensor or per-head — halves HBM for KV
- **INT4 KV**: per-channel — quarters it, larger accuracy hit
- **KVFP4**: FP4 KV, usually Blackwell-only

## Checkpoint formats

| Format | Covers | Where |
|:-------|:-------|:------|
| **HF `quantization_config`** | AWQ, GPTQ, bitsandbytes, FP8, GGUF | top-level `config.json` |
| **compressed-tensors** | most (W, W+A, KV, MoE, sparsity) | Neural Magic standard |
| **auto-awq** | AWQ | Casper Hansen's lib |
| **auto-gptq** | GPTQ | |
| **GGUF** | GGUF family | llama.cpp |
| **NVIDIA ModelOpt** | FP8/FP4 | TensorRT-LLM / vLLM |

## Compatibility

| Scheme | vLLM | SGLang | TRT-LLM | HW floor |
|:-------|:-----|:-------|:--------|:---------|
| FP8 per-tensor W+A | ✓ | ✓ | ✓ | Hopper+ |
| FP8 block (DeepSeek 1×128 / 128×128) | ✓ | ✓ | ✓ | Hopper+ |
| AWQ INT4 | ✓ | ✓ | ✓ | Ampere+ |
| GPTQ INT4 | ✓ | ✓ | ✓ | Ampere+ |
| Marlin (AWQ/GPTQ kernel) | ✓ | ✓ | via CUTLASS | Ampere+ |
| MXFP4 | ✓ (mxfp4.py) | ✓ (mxfp4.py) | ✓ | portable |
| NVFP4 | ✓ (modelopt) | ✓ (modelopt_quant) | ✓ (fp4_utils) | Blackwell |
| GGUF | ✓ | ✓ | — | CPU or GPU |
| bitsandbytes (nf4 / int8) | ✓ | ✓ | — | wide |
| KV cache FP8 | ✓ (kv_cache.py) | ✓ (kv_cache.py) | ✓ | Hopper+ |
| KVFP4 | — | ✓ (kvfp4_tensor.py) | ✓ | Blackwell |

## Engine pointers

| Engine | Quantization root |
|:-------|:------------------|
| vLLM | `vllm/model_executor/layers/quantization/` — one file per scheme (`awq.py`, `awq_marlin.py`, `gptq.py`, `gptq_marlin.py`, `fp8.py`, `mxfp4.py`, `modelopt.py`, `fp_quant.py`, `bitsandbytes.py`, `gguf.py`, `kv_cache.py`, `input_quant_fp8.py`, `compressed_tensors/`, `quark/`, `torchao/`, `turboquant/`) |
| SGLang | `python/sglang/srt/layers/quantization/` — one file per scheme (`awq.py`, `gptq.py`, `fp8.py`, `fp8_kernel.py`, `blockwise_int8.py`, `int8_kernel.py`, `mxfp4.py`, `fp4_utils.py`, `kv_cache.py`, `kvfp4_tensor.py`, `modelopt_quant.py`, `petit.py`, `compressed_tensors/`, `configs/`) |
| TRT-LLM | `tensorrt_llm/quantization/` — `mode.py`, `functional.py`, `layers.py`, `fp8_quantize.py`, `fp4_utils.py` |

## Pitfalls

- **Calibration mismatch.** Loading an AWQ checkpoint without the `quantization_config` block silently runs unquantized and OOMs.
- **Activation scale range.** Per-tensor FP8 activations overflow on unusual prompts; per-channel or per-token scales avoid it.
- **MoE quantization.** Experts quantized but router kept FP16 is typical; applying quant to the router almost always hurts accuracy.
- **Block FP8 alignment.** DeepSeek 128×128 block requires hidden/intermediate dims divisible by 128.
- **KV quant + radix cache.** Cache key must include the quant scheme; otherwise a reuse across schemes silently corrupts decode.
- **Marlin layout is the kernel, not the data.** AWQ data must be repacked before a Marlin kernel reads it — don't confuse GPTQ-Marlin as a distinct scheme.
- **Speculative drafts at higher precision.** Mixing FP16 draft + FP8 target is fine for draft-model spec; MTP / EAGLE heads share the target's precision.

## See also

- [`models/text-moe/`](../models/text-moe.md) — FP8 block quant is the native format for DeepSeek
- `algorithms/moe-routing-dispatch/` — MoE-specific quant kernels
- `backends/flashinfer/`, `backends/flashattention/` — quant-aware attention kernels
- `hardware/nvidia/` — precision support by generation (Hopper FP8, Blackwell FP4)
