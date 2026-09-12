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

Hardware support is generation-gated, so the floor column is the load-bearing
one. Backend values are exact `ComputeBackend` names.

| Scheme | vLLM | SGLang | TRT-LLM | HW floor |
|:-------|:-----|:-------|:--------|:---------|
| FP8 per-tensor W+A | ✓ | ✓ | ✓ | Hopper+ |
| FP8 block (DeepSeek 1×128 / 128×128) | ✓ | ✓ | ✓ | Hopper+ |
| AWQ INT4 | ✓ | ✓ | ✓ | Ampere+ |
| GPTQ INT4 | ✓ | ✓ | ✓ | Ampere+ |
| Marlin (AWQ/GPTQ kernel) | ✓ | ✓ | via CUTLASS | Ampere+ |
| MXFP4, native tensor path | ✓ (mxfp4.py) | ✓ (mxfp4.py) | ✓ | gfx950 (CDNA4), `cuda` Blackwell |
| MXFP4 on gfx942 (`rocm`, MI300A/MI300X) | ✓ (mxfp4.py) | ✓ (mxfp4.py) | — | **N/A** native tensor path; weight-only via a dequant-in-kernel fallback |
| NVFP4 | ✓ (modelopt) | ✓ (modelopt_quant) | ✓ (fp4_utils) | Blackwell |
| GGUF | ✓ | ✓ | — | CPU or GPU |
| bitsandbytes (nf4 / int8) | ✓ | ✓ | — | wide |
| KV cache FP8 | ✓ (kv_cache.py) | ✓ (kv_cache.py) | ✓ | Hopper+ |
| KVFP4 | — | ✓ (kvfp4_tensor.py) | ✓ | `cuda` Blackwell |

### Non-CUDA backends

| Backend | Native path | Notes |
|:--|:--|:--|
| `rocm` | FP8 (E4M3/E5M2), INT8, INT4 weight-only | FP8 native from MI300 (CDNA3, gfx942). MXFP4 is native only from CDNA4 (gfx950); on gfx942 (MI300A, MI300X) there is no native MXFP4 tensor path, so MXFP4 checkpoints run weight-only via a dequant-in-kernel fallback. Kernel coverage is narrower than NVIDIA: confirm the scheme is implemented before designing around it. |
| `metal` | `mx.quantize` group-wise INT4 / INT8 | No external toolchain; the fast kernels consume quantized weights directly. **The highest-leverage optimization on this backend** — decode is bandwidth-bound, so fewer bytes per token converts almost linearly to tokens/sec. Group size 64 is a reasonable default. No FP8/FP4. |
| `trainium` | BF16 default; FP8 on Trn2 | Quantization is secondary here — the decisive decode win is the device-resident KV cache, not precision. |
| `cpu` | INT8 / INT4 weight-only, GGUF (Q4_K_M, Q5_K_S) | Largest single win on CPU: decode is bandwidth-bound and the arithmetic units are narrow. |

Status: verified (MXFP4 gfx942/gfx950 split, observed and explained by mechanism read in aiter source; the gate was reconfirmed at the source level, `is_fp4_avail` scoped to gfx950/gfx1250, in aiter `d9e5ef7ce0`). Scope: `rocm`, gfx942 vs gfx950. sglang-v0.5.18-rocm700-mi30x, 2026-09-05 to 2026-09-11, job 623402, aiter d9e5ef7ce0.

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
- **Integer MXFP4 decode is wrong at the subnormal edge of the block-exponent range.** An integer decode that adds the MXFP4 block exponent directly into the bf16 exponent field (skipping a float `ldexpf`) is bit-exact only for block exponents in [-125, 125]; outside that range specific magnitudes fall into bf16 subnormal territory, where the result must itself be a subnormal bit pattern and the naive integer add instead returns zero. Real checkpoints reach this edge: `amd/Qwen3.5-397B-A17B-MXFP4` has block_exp = -126 (the boundary value) in about 3 percent of e8m0 scale blocks, confirmed wrong on device without a guard. Any integer MXFP4 decode needs a guard or an explicit subnormal path; the guard's own cost can exceed what the integer decode saves over a float `ldexpf`. Scope: MXFP4 e8m0 block scales, any backend. Status: verified (exhaustive on-device check across scale-byte and block-exponent ranges). Stamp: sglang-v0.5.18 fork, 2026-09-11, job 633021.
- **Dequantised scratch buffers don't automatically beat an in-kernel-dequant fused kernel.** Splitting dequant into its own pass (write a dequantised scratch, then run a native-precision GEMM against it) changes total bytes moved by the format's compression ratio, not by the ratio of compute costs; it wins only when the fused dequant kernel is itself compute-bound (so the fused path's ALU cost, not its bandwidth, is what a scratch split relieves) and the standalone dequant kernel plus the scratch GEMM together land near their own roofline. Measured otherwise on `rocm`, gfx942: a dequant-to-bf16-scratch scheme for a routed MXFP4 MoE lost to the fused kernel at every prefill M tested, because the standalone dequant kernel itself reached only about 18 percent of HBM peak, not because the arithmetic was wrong. Scope: any format where a fused kernel already decodes in-register; verify per kernel and per shape before assuming a split wins. See `platforms/` for the numbers this generalizes from.
- **Numerics-changing swaps need an agreement test, not a per-layer bound.** A CK fp8 a8w8 MoE kernel swapped in for MXFP4 experts passed a per-layer numerics check (about 4.5 percent relative L2 error) and holdout-session accuracy probes, but on a 60-layer hybrid MoE model under multi-turn decoding, the same build's history and arithmetic probes returned repeated "!" tokens instead of content (6 of 13 probes failing). Cause: per-token fp8 activation quantization plus per-channel fp8 weight scaling of the dequantized MXFP4 experts compounds across decode layers; the error is invisible at one layer and only surfaces after many layers of drift. Fix: treat any numerics-changing candidate (a new quantization scheme, a re-quantized checkpoint) as requiring an agreement test against the current path on real multi-turn conversations (top-1 token agreement and KL divergence) before acceptance, or exclude such candidates by policy; a per-layer error under 5 percent is not evidence of end-to-end acceptability, and neither is a coarse pass/fail probe gate on its own. Scope: backend-independent mechanism (precision-swap error compounding over decode length); observed on `rocm` gfx942 with aiter's CK fp8 a8w8 MoE kernel. Status: verified (gate failure reproduced across probe types in one run; mechanism documented). Stamp: sglang-v0.5.18 fork, 2026-09-11, job 632232.

## See also

- [`models/text-moe/`](../models/text-moe.md) — FP8 block quant is the native format for DeepSeek
- `algorithms/moe-routing-dispatch/` — MoE-specific quant kernels
- **FlashInfer**, **FlashAttention** — quant-aware attention kernels
- your platform's `hardware.md` — precision support by generation (Hopper FP8, Blackwell FP4)
