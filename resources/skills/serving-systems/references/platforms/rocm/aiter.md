# AITER and Composable Kernel

The fused-kernel layer on CDNA — the ROCm answer to the question [`attention-backend-comparison.md`](../cuda/attention-backend-comparison.md) answers for NVIDIA. Consuming these libraries, not writing kernels.

> **Status: experimental.** Not exercised end to end against MI300-class hardware in this repo. Verify API surface and coverage against your ROCm and library versions before designing around a specific kernel.

## The stack

```
AITER              — AMD's fused-op library (attention, GEMM, MoE, norm).
                     Its kernels are written in a mix of Triton, Composable
                     Kernel, HIP, and hand-written assembly — several of the
                     fastest paths (MoE, FMHA) are ASM. Triton lives *inside*
                     AITER, not beside it.
   ↓ drops to
Composable Kernel  — templated kernel framework for CDNA; usable directly
   ↓ or
Triton             — portable; also how vLLM/SGLang cover ROCm gaps
   ↓ floor
PyTorch SDPA       — always available, fused, slowest of the four
```

## Picking

| Situation | Use |
|:--|:--|
| Standard causal MHA/GQA attention, common head dims | AITER |
| A variant AITER doesn't cover | Composable Kernel directly, or Triton |
| Portability with a CUDA build from one source | Triton — see [`frameworks/triton.md`](../../frameworks/triton.md) |
| Bring-up, or nothing else works | SDPA |

The decision that matters is not which is fastest in isolation but **which actually covers your variant**. Coverage on CDNA is narrower than on NVIDIA; a variant that silently falls back to SDPA will look like a hardware deficit when it is a kernel-selection problem.

## Verify which kernel ran

The single most useful habit on this backend. A fallback is silent and presents as "AMD is slow."

Check via the engine's backend-selection logging, or profile and confirm the kernel names on the timeline match the library you intended — see [`profiler.md`](profiler.md).

Do this before concluding anything about relative hardware performance. For the full engagement-proof recipe (`AITER_LOG_TUNED_CONFIG`, the per-shape lookup key, and why offline `hipBLASLt`/`TunableOp` tuning often doesn't reach the live dispatch), see [`aiter-engagement.md`](aiter-engagement.md).

## Paged KV

The paged-attention design transfers unmodified from [`algorithms/paged-attention.md`](../../algorithms/paged-attention.md) — block pool, page table, batch arrays. What varies is which kernels accept a block table. Triton paged attention is the portable path and is what both vLLM and SGLang use to cover ROCm.

## Engine support

| Engine | ROCm |
|:--|:--|
| vLLM | supported; ROCm-specific kernels under the fused-MoE and attention backends |
| SGLang | supported |
| TensorRT-LLM | not supported — NVIDIA only |

The engine source maps in [`engines/`](../../engines/) are written against NVIDIA-first trees; ROCm paths exist within vLLM and SGLang but are not the primary codepath, so expect thinner coverage of edge variants.

## Pitfalls

- **Assuming a FlashAttention/FlashInfer API.** Different libraries. The *algorithm* is available; the call is not.
- **Not checking coverage before designing.** Build around a variant AITER doesn't implement and you land on SDPA.
- **Porting CUDA head-dim or block-size assumptions.** Tile shapes are tuned for CDNA; re-tune rather than inheriting Hopper-era constants.
- **Treating a fallback as a hardware result.** Confirm the kernel first.

## Out of scope — kernel implementation

Writing new CDNA kernels (HIP, CK templates) is outside this collection. This file covers consuming existing libraries.

## See also

- [`aiter-engagement.md`](aiter-engagement.md) — prove a tuning or dispatch change reached the live server before trusting a measured delta
- [`floor.md`](floor.md) — where the fused kernel sits in the optimization floor
- [`hardware.md`](hardware.md) — CDNA3/CDNA4 precision support and GFX IDs
- [`frameworks/triton.md`](../../frameworks/triton.md) — the portable fallback
