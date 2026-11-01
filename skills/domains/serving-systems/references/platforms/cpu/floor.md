# CPU optimization floor

No accelerator. Every technique whose premise is "hide host work behind device work" or "reduce transfers across the device boundary" is vacuous here. What remains is memory bandwidth, cache behavior, threading, and arithmetic width.

> **Status: thin.** `ComputeBackend.CPU` is a supported, runnable backend, but this collection's depth is in accelerator serving. The guidance below is the floor, not a complete treatment.

## 1. Threading discipline

The dominant cost at batch 1 is usually not FLOPs but thread contention and scheduling.

- Set the intra-op thread count explicitly; the default is usually the core count including SMT siblings, which oversubscribes.
- Pin threads and respect NUMA boundaries — a matmul whose weights live on the far socket pays for it on every token.
- One inference thread pool, not one per request. Oversubscription across concurrent requests is the most common CPU serving mistake.

## 2. Quantization

The largest single win, because CPU decode is bandwidth-bound and the arithmetic units are narrow.

- INT8 and INT4 weight-only are the workhorses; GGUF's Q4_K_M / Q5_K_S family is the well-trodden path.
- See [`algorithms/quantization-schemes.md`](../../algorithms/quantization-schemes.md) — the GGUF row is the relevant one.

## 3. Instruction width

Verify the build actually uses the widest ISA available (AVX-512, AMX on recent Xeon, NEON/SVE on ARM). A generic build silently leaves several-fold performance unclaimed. This is a build-configuration check, not a code change, and it is worth making first.

## 4. Batching still helps — for a different reason

Batching on an accelerator amortizes launch overhead and fills parallel units. On CPU it amortizes *weight reads*: the same weight row serves multiple sequences per pass, converting a bandwidth-bound decode into something closer to compute-bound.

- Contract: [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md). The invariants hold; there is no separate device pool, so the KV strategy resembles the `metal` case more than the `cuda` one.

## What does not apply

| Technique | On CPU |
|:--|:--|
| Graph capture (CUDA/HIP graphs) | N/A — no launch overhead to eliminate |
| Paged attention / block pool | N/A — no discrete device pool |
| Accelerator kernel libraries (FlashAttention / FlashInfer / AITER) | N/A — but **not** "no fused attention": use `torch.nn.functional.scaled_dot_product_attention`, which has a CPU flash-attention backend (fp32/bf16). oneDNN/IPEX and llama.cpp fuse too. Never materialize the full score matrix on CPU either. |
| Async / overlap scheduling | N/A — there is no device to overlap host work against |
| TP / PP / EP, disaggregated serving | N/A within one host |

## See also

- [`hardware.md`](hardware.md) — what to check about the target CPU
- [`profiler.md`](profiler.md) — `perf` / Instruments
- [`tooling/serving-benchmark.md`](../../tooling/serving-benchmark.md) — measurement discipline is unchanged
