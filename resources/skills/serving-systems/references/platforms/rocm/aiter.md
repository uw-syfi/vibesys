# AITER and Composable Kernel

The fused-kernel layer on CDNA: the ROCm answer to the attention-backend-picking question that CUDA's `attention-backend-comparison.md` answers for NVIDIA (see the [`platforms/`](../) directory for that backend's files). Consuming these libraries, not writing kernels.

> **Status:** the capability table and JIT-cache workflow below are verified on gfx942 (MI300A), `sglang-v0.5.18-rocm700-mi30x` image, 2026-08-25 to 2026-09-10. Other gfx targets and other images are not verified here; check your build before relying on a specific kernel path.

## The stack

```
AITER             : AMD's fused-op library (attention, GEMM, MoE, norm).
                     Its kernels are written in a mix of Triton, Composable
                     Kernel, HIP, and hand-written assembly: several of the
                     fastest paths (MoE, FMHA) are ASM. Triton lives *inside*
                     AITER, not beside it.
   ↓ drops to
Composable Kernel : templated kernel framework for CDNA; usable directly
   ↓ or
Triton            : portable; also how vLLM/SGLang cover ROCm gaps
   ↓ floor
PyTorch SDPA      : always available, fused, slowest of the four
```

## Picking

| Situation | Use |
|:--|:--|
| Standard causal MHA/GQA attention, common head dims | AITER |
| A variant AITER doesn't cover | Composable Kernel directly, or Triton |
| Portability with a CUDA build from one source | Triton: see [`frameworks/triton.md`](../../frameworks/triton.md) |
| Bring-up, or nothing else works | SDPA |

The decision that matters is not which is fastest in isolation but **which actually covers your variant**. Coverage on CDNA is narrower than on NVIDIA; a variant that silently falls back to SDPA will look like a hardware deficit when it is a kernel-selection problem.

## Capability by gfx target (sglang-v0.5.18-rocm700 image)

The aiter build bundled with the `sglang-v0.5.18-rocm700-mi30x` image.

| Path | gfx942 (MI300A / MI300X) | gfx950 (MI350X / MI355X) |
|:--|:--|:--|
| Unified attention / `mha_batch_prefill` | Works | Not exercised here |
| MXFP4 MoE fused quant+sort (`fused_dynamic_mx_quant_moe_sort_hip`) | Rejects fp4x2 output: `"not support output type: fp4x2"` (`quant_kernels.cu:1987`) | Targets this generation |
| CK a4w4 2-stage GEMM | Not built (device code compiled for gfx950 only) | Works |
| FlyDSL a4w4 | Fails to compile | Targets this generation |

Net: no AITER MXFP4 MoE path on gfx942. `AITER_FLYDSL_FORCE=1` in the launch recipe (see [`floor.md`](floor.md)) has no effect on gfx942 for this reason.

Scope: gfx942, gfx950. Status: verified (gfx942 rows; gfx950 rows are the kernel's stated target, not independently exercised in this campaign). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25 to 2026-09-10.

### gfx942 MXFP4 MoE workaround: Triton w4a16 fallback

No AITER MXFP4 MoE path exists on gfx942 (table above), so the fork carries a Triton fused-MoE w4a16 kernel: bf16 activations with in-kernel MXFP4 dequant, auto-selected on HIP builds without gfx95 support. Opt out with `SGLANG_MXFP4_MOE_TRITON_FALLBACK=0`.

- Untuned for gfx942.
- Validated: server boots, CUDA-graph capture completes over 52 batch sizes, greedy and history probes pass, ~28 tok/s single stream.
- **Candidate:** the ~106 ms decode-step (TPOT) time observed in production serving is bottlenecked by these MoE grouped GEMMs. Not profiled; a roofline for 17B active params at 4 bits over 4 devices is under 10 ms. Would verify with a kernel-level profile (`rocprof-compute`) isolating MoE GEMM time in the decode step.

Scope: rocm, gfx942, `sglang-v0.5.18-rocm700-mi30x` with aiter bundled. Status: verified (fallback mechanism and correctness); candidate (decode-step bottleneck attribution). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-05 to 2026-09-10, uw-syfi/sglang commit b0e13701b9.

## JIT cache

AITER JIT-builds kernels into `AITER_JIT_DIR` (default: inside the aiter package directory, which in a squashfs or otherwise read-only container image is ephemeral or read-only). Set it to a persistent, pre-warmed directory before the first launch.

Warm-up procedure:

1. Set `AITER_JIT_DIR` to a persistent path.
2. Launch the server once with the production flags. Expect a cold boot: JIT builds run serialized.
3. Relaunch with the same flags and the same `AITER_JIT_DIR`. This is the warm boot.
4. Confirm the cache holds 8 top-level `.so` files, about 59 MB total.
5. Redo this warm-up after any aiter or sglang version bump; a signature change silently falls back to a cold build.

Measured: cold boot 12 m 36 s (about 5 min of serialized JIT builds after a 4 m 26 s load); warm relaunch 7 m 24 s, zero lock waits.

Scope: rocm, gfx942, `sglang-v0.5.18-rocm700-mi30x`. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-08-28.

## Verify which kernel ran

The single most useful habit on this backend. A fallback is silent and presents as "AMD is slow."

Check via the engine's backend-selection logging, or profile and confirm the kernel names on the timeline match the library you intended: see [`profiler.md`](profiler.md).

Do this before concluding anything about relative hardware performance.

## Paged KV

The paged-attention design transfers unmodified from [`algorithms/paged-attention.md`](../../algorithms/paged-attention.md): block pool, page table, batch arrays. What varies is which kernels accept a block table. Triton paged attention is the portable path and is what both vLLM and SGLang use to cover ROCm.

## Engine support

| Engine | ROCm |
|:--|:--|
| vLLM | supported; ROCm-specific kernels under the fused-MoE and attention backends |
| SGLang | supported |
| TensorRT-LLM | not supported: NVIDIA only |

The engine source maps in [`engines/`](../../engines/) are written against NVIDIA-first trees; ROCm paths exist within vLLM and SGLang but are not the primary codepath, so expect thinner coverage of edge variants.

## Pitfalls

- **Assuming a FlashAttention/FlashInfer API.** Different libraries. The *algorithm* is available; the call is not.
- **Not checking coverage before designing.** Build around a variant AITER doesn't implement and you land on SDPA.
- **Porting CUDA head-dim or block-size assumptions.** Tile shapes are tuned for CDNA; re-tune rather than inheriting Hopper-era constants.
- **Treating a fallback as a hardware result.** Confirm the kernel first.

### Stale JIT lock after a killed launch

```
Symptom: a later launch sharing the same AITER_JIT_DIR hangs forever; log
         shows "waiting for baton release".
Cause:   a previous launch was killed mid-build and left a file_baton lock
         in AITER_JIT_DIR.
Fix:     never share a JIT dir across a killed launch; keep a clean copy
         of the warm cache to fall back to (deleting the lock may not be
         permitted on shared storage).
Scope:   rocm, gfx942, sglang-v0.5.18-rocm700-mi30x with aiter bundled.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-08-28.
```

### Lazy kernel-variant build kills health check on first request

```
Symptom: server exits shortly after the first real request; log shows a
         JIT build of a kernel variant warmup did not exercise (observed:
         mha_batch_prefill_bf16_..._nmask_..., ~105 s build); the first
         benchmark's turn-1 TTFT is contaminated (2672 ms).
Cause:   AITER builds kernel variants lazily on first use, even with a
         warm cache. SGLang's HTTP server kills itself when the
         detokenizer stalls longer than SGLANG_HEALTH_CHECK_TIMEOUT
         (default 20 s), so the build looks like a dead server.
Fix:     set SGLANG_HEALTH_CHECK_TIMEOUT=1800; treat the first request
         after boot as warmup, not as a measurement.
Scope:   rocm, gfx942, sglang-v0.5.18-rocm700-mi30x with aiter bundled.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, job 623402.
```

## Out of scope: kernel implementation

Writing new CDNA kernels (HIP, CK templates) is outside this collection. This file covers consuming existing libraries.

## See also

- [`floor.md`](floor.md): where the fused kernel sits in the optimization floor, and the validated launch recipe
- [`hardware.md`](hardware.md): CDNA3/CDNA4 precision support and GFX IDs
- [`unified-memory.md`](unified-memory.md): the mem_fraction_static x0.85 multiplier this library applies, and its consequence for save-time memory math
- [`weight-loading.md`](weight-loading.md): checkpoint materialization, independent of which attention/MoE kernel is selected
- [`frameworks/triton.md`](../../frameworks/triton.md): the portable fallback
