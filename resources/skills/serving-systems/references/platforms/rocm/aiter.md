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

The gate is unconditional at the source level: every MXFP4-weight kernel in this aiter build (commit `d9e5ef7ce0`) is behind an `is_fp4_avail` check scoped to `gfx950`/`gfx1250`. On `gfx942` the Quark MXFP4 checkpoint scheme (`quark_w4a4_mxfp4_moe.py`) therefore always takes the sglang Triton fallback; `SGLANG_MXFP4_MOE_TRITON_FALLBACK` defaults to `1` and does not need to be set explicitly on this platform. The aiter a4w4 path, if forced, aborts with the `fused_dynamic_mx_quant_moe_sort_hip: not support output type: fp4x2` error above rather than silently degrading.

Scope: gfx942, gfx950. Status: verified (gfx942 rows and the `is_fp4_avail` gate, read from aiter source and reproduced twice; gfx950/gfx1250 rows are the kernel's stated target, not independently exercised in this campaign). Stamp: `sglang-v0.5.18-rocm700-mi30x`, aiter `d9e5ef7ce0`, 2026-08-25 to 2026-09-11.

### gfx942 MXFP4 MoE workaround: Triton w4a16 fallback

No AITER MXFP4 MoE path exists on gfx942 (table above), so the fork carries a Triton fused-MoE w4a16 kernel: bf16 activations with in-kernel MXFP4 dequant, auto-selected on HIP builds without gfx95 support. Opt out with `SGLANG_MXFP4_MOE_TRITON_FALLBACK=0`.

- Untuned for gfx942.
- Validated: server boots, CUDA-graph capture completes over 52 batch sizes, greedy and history probes pass, ~28 tok/s single stream.

Decode-step decomposition (torch profiler, 16 decode steps at batch size 15), reproduced in two separate jobs:

| Component | Share of decode-step GPU time |
|:--|:--|
| MoE (Triton `fused_moe_kernel_gptq_awq` + routing) | 61 to 64 percent |
| Dense GEMMs (hipBLASLt default Tensile kernels) | 30 to 31 percent |
| All-reduce | ~2 percent |
| Attention, Gated DeltaNet, other | ~3 percent |

The MoE kernel is the largest single term but not the whole story: the untuned dense-GEMM fallback path (see the tuned-GEMM pitfall below) is close behind it. GPU idle time during the step was under 1 percent, so this is a compute/kernel-selection problem, not a scheduling gap.

Scope: rocm, gfx942, `sglang-v0.5.18-rocm700-mi30x` with aiter bundled. Status: verified (reproduced in 2 jobs). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11, jobs 631857 and 631900.

### aiter's CK fused MoE kernels on gfx942 (not MXFP4, but faster)

aiter's Composable Kernel fused-MoE kernels are not gated to gfx950 the way the MXFP4 path is (see the capability table above); they build and run on gfx942, just at a different weight precision than the production MXFP4 checkpoint. One device, E=512, top-10, K=4096, per-rank N=256 (the production shape):

| Kernel | ms/layer at M=16 | ms/layer at M=1024 | Achieved bandwidth |
|:--|:--|:--|:--|
| CK fp8 a8w8 (activation quant included) | 0.48 | 1.06 | 950 to 1600 GB/s |
| CK bf16 a16w16 | 0.54 | 1.52 | 950 to 1600 GB/s |

Both are several times faster than the production MXFP4 Triton kernel at the same shapes. The catch is memory, not speed: fp8 experts held resident would need about 97 GB per device (388 GB across the 4-device node) against roughly 430 GB free, so a full resident fp8 swap does not fit. See [`models/qwen3-5.md`](../../models/qwen3-5.md) for the resident/dequant hybrid design this motivates.

Scope: rocm, gfx942, aiter `d9e5ef7ce0`. Status: verified. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11, jobs 631892 and 631902.

### No shipped kernel consumes MXFP4 weights directly on gfx942

Beyond the AITER MXFP4 gate in the capability table above, every other shipped kernel path was checked and also does not consume MXFP4 weights on gfx942:

| Library / path | Finding | Evidence |
|:--|:--|:--|
| CK MX-GEMM grouped-GEMM (`device_moe_mx_gemm_bpreshuffle.hpp`) | Block pipeline requires gfx950's native scaled-MFMA; a hardware gap in the pipeline, not a missing build flag | `blockwise_gemm_mx_pipeline_xdlops_base.hpp:88-96` |
| aiter's CK codegen | Every generated instance whose B dtype contains `FP4` is wrapped in `#ifndef __gfx942__` | `ck_gemm_moe_2stages_codegen/gen_instances.py:945-948` |
| aiter Triton MXFP4 (`tl.dot_scaled`) | Gated to `gfx950`/`gfx1250` via `is_fp4_avail()`; no gfx942 codepath compiles at all | `arch_info.py:19-20`, `moe_op_gemm_a16w4.py:305-306,335-336` |
| aiter FlyDSL a16w4 software decode | Portable code exists (`_unpack_b_mxfp4_bf16_sw`), but costs about 18 VALU per element on gfx942 versus about 0.5 on gfx950's hardware convert instruction; its test suite hard-skips off gfx950 | `mfma_preshuffle_pipeline.py:1221-1223`; `test_flydsl_moe_a16wfp4.py:60-63` |
| aiter CK-Tile `a16w4_mxfp4_swiglu` | ImportError (undefined symbol) even after rebuilding into a writable JIT cache; the swiglu instantiation never appears in `build.ninja` although its `.cuh` source exists, a build-manifest gap, not an architecture gate | aiter `d9e5ef7ce0` |

Scope: gfx942, aiter `d9e5ef7ce0`, CK `f33252ce`. Status: verified (mechanism read from source at every row; the CK-Tile row also reproduced live). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11.

### From-scratch HIP fused w4a16 MoE kernel: bypasses the gap above

A HIP kernel that decodes e2m1 (MXFP4) nibbles via a 16-entry LUT plus an exponent add directly into `v_mfma_f32_16x16x16_bf16` B fragments works on gfx942, with no separate dequant pass and no CK weight-preshuffle step:

- Decode: nibble extract (shift+mask) then LUT lookup then `ldexpf` exponent-field add then cast to bf16, about 5 to 6 VALU per element. The MFMA operand mapping (lane `l` holds block `l/16`, row/col `l%16`) is verified against `ck/tensor_operation/gpu/warp/xdlops_gemm.hpp:471-490,2740-2825`.
- Each lane owns one contiguous 256-wide K run (rather than re-reading the same 32 bytes per MFMA window), so one 16-byte `buffer_load_dwordx4` per lane covers 32 K-values plus their e8m0 scale block.
- Decode is not the marginal cost: the fused kernel is at wall-clock parity with a pre-decoded-bf16 version of the same loop (0.88 to 1.63x across shapes) while moving 3.77x fewer bytes.
- Templating `K` and `NWAVES` as compile-time (not runtime) kernel parameters keeps the load-prefetch ring in registers; a runtime-`K` version spilled 48 to 176 bytes per lane to scratch. Best configs: WIDE 4 column tiles sharing one A walk, 8 waves for K=4096 (gate_up), 2 waves for K=256 (down).
- Measured per layer (one device, E=512, top-10, K=4096, per-rank N=256, the production shape), M=16, at the decode-step touched-expert count E=138 and the prefill-like expert count E=512 (M still 16): 0.39 ms (E=138) and 1.54 ms (E=512), versus production's Triton kernel at 1.45 ms and 5.2 ms. Decode is fully fused into the MFMA feed, with no separate memory pass.
- Integrated into the fork's grouped MoE kernel and validated against real layer-30 checkpoint weights across production M values: 0.554 ms/layer at M=16 up to 3.545 ms/layer at M=1024, versus production's 1.468 to 5.212 ms (1.47x to 2.65x faster). Correctness: rel L2 about 2e-3 against an fp32 reference at every M tested (production sits at about 4e-3).
- Under the real multi-turn benchmark this kernel alone (`SGLANG_MXFP4_MOE_HIP=1`) cut mean TPOT from 106.82 ms to 61.00 ms (-42.9 percent) and p95 TTFT turn-2+ from 784.7 ms to 537.5 ms (-31.5 percent); see [`models/qwen3-5.md`](../../models/qwen3-5.md) and [`tooling/serving-benchmark.md`](../../tooling/serving-benchmark.md).

Env var: `SGLANG_MXFP4_MOE_HIP=1` selects this kernel over the Triton fallback above; see [`floor.md`](floor.md).

Scope: rocm, gfx942, sglang-v0.5.18 fork (`moe/mxfp4-fused`, PR #19). Status: verified (reproduced across the single-tile microbenchmark, the grouped-kernel checkpoint-weight validation, and the end-to-end benchmark). Stamp: `sglang-v0.5.18-rocm700-mi30x`, rocm 7.0, 2026-09-11, jobs 632237, 632241/2, 632253, 632489.

### Stage1 dispatch by sorted-block count closes a scaffold-vs-templated gap at low M

Stage1 (gate_up) of the fused kernel above dispatches per launch by sorted-block count: the scaffold loop wins at every point below 1024 sorted blocks, and the templated 8-wave loop wins at or above (a lighter templated variant, tried as an alternative, did not beat the scaffold loop below that point either). Shipped rule: scaffold below 1024 blocks, templated at or above.

Stage1 time, old dispatch to new: M=16 0.4022 to 0.3042 ms, M=32 0.6497 to 0.5209 ms, M=48 0.8817 to 0.6997 ms, M=64 1.0292 to 0.8341 ms, M=256 1.4314 to 1.2867 ms, M=1024 unchanged (already on the templated side of the threshold). Total per layer: M=16 minus 18 percent (0.5412 to 0.4414 ms), M=64 minus 14 percent, M=256 minus 8 percent. Same kernels, dispatch decision only, so correctness is unchanged: rel L2 2.3 to 2.5e-3 against an fp32 reference at every M tested, and CUDA-graph replay is bit-exact.

Scope: rocm, gfx942, sglang-v0.5.18 fork (`moe/stage1-small-m`, PR #40). Status: verified (measured on real layer-30 rank-0 weights across the production M range; 11 unit tests pass). Stamp: `sglang-v0.5.18-rocm700-mi30x`, rocm 7.0, 2026-09-11, job 632917.

### Custom skinny bf16 GEMM closes most of the tuned-GEMM gap at decode M

The tuned-GEMM pitfall below shows aiter's own tuner reaching only 3 to 10 percent of peak at M=16 on the model's six dense-projection shapes, because 256-wide hipBLASLt tiles leave most of MI300A's 228 CUs idle at these skinny M values. A HIP kernel purpose-built for this M range, using one workgroup per slice of `w`'s rows, 16-byte loads, fp32 accumulation, wave-shuffle (`__shfl_xor`) reduction, and split-K for the small-N shapes, reaches up to 1018 GB/s (about 19 percent of the 5.3 TB/s peak) on the same shapes, 1.8x to 8.7x faster per call than hipBLASLt's default kernel at M=15/16 across all six shapes (best-config sweep; the kernel's own default-heuristic config under-picks the row-tile width and lands up to 1.4x below the sweep best on 5 of 6 shapes). aiter's own `wvSpltK` kernel only covers M=1 to 4, so it is not a competing option at decode M=15/16.

Under the real multi-turn benchmark, stacking this kernel (`SGLANG_SKINNY_GEMM=1`) on top of the fused MoE kernel above cut mean TPOT a further 25.4 ms (61.0 to 35.6 ms, -41.6 percent) and p95 TTFT turn-2+ a further 78.7 ms (537.5 to 458.8 ms, -14.6 percent), for a combined 66.6 percent TPOT cut and 41.5 percent p95 TTFT cut against the pre-both-kernels baseline.

Env var: `SGLANG_SKINNY_GEMM=1`, routed for M <= 16; see [`floor.md`](floor.md).

Scope: rocm, gfx942, MI300A (228 CU), rocm 7.0. Status: verified (single-kernel microbenchmark reproduced across the M=16 sweep and the M=1 comparison; end-to-end effect reproduced in the three-side paired benchmark). Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11, jobs 632226, 632230/632233, 632238, 632503.

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

### Dequant Triton kernels measure at 4-5 percent of HBM bandwidth (not bandwidth-bound)

```
Symptom: a Triton MXFP4/int4 dequant-and-GEMM kernel (observed in three:
         sglang's fused_moe_kernel_gptq_awq, aiter's Triton int4 MoE, and
         a streaming dequant kernel) profiles at 4-5 percent of peak HBM
         bandwidth, and tuning the launch config (block/tile sizes) does
         not recover more than 3 percent.
Cause:   counters show these kernels are VALU-heavy with about 7 wait
         cycles per VALU instruction; duration tracks the K-loop length,
         not bytes moved. They are latency-bound on in-kernel dequant
         arithmetic, not bandwidth-bound, so bandwidth-oriented tiling
         changes don't move the needle.
Fix:     no tiling fix exists for this kernel shape. Treat MXFP4/int4
         Triton dequant-in-kernel kernels on gfx942 as VALU-latency-bound
         by default; a real speedup needs a different kernel or a
         different weight format (e.g. a native-precision kernel, see the
         CK fused-MoE numbers above), not a retuned Triton config.
Scope:   rocm, gfx942, triton 3.4.0, sglang-v0.5.18-rocm700.
Status:  verified as a pattern (3 kernels, reproduced across 2 jobs);
         candidate on the precise microarchitectural root cause (VALU
         latency is the confirmed symptom, not yet isolated to a specific
         instruction mix). sglang-v0.5.18-rocm700-mi30x, 2026-09-11, jobs
         631877 and 631890.
```

### aiter's tuned-GEMM table misses every dense projection on MI300A

```
Symptom: dense projection GEMMs fall back to hipBLASLt default (Tensile)
         kernels; log carries lines like "[aiter] not found tuned config
         ... will use default config" for every dense shape in the model
         (thousands of times per run); the default-config kernels reach
         only 3 to 10 percent of peak at small M (skinny decode shapes).
Cause:   aiter's tuned-GEMM lookup is keyed on (gfx target, cu_num,
         padded_M, N, K, ...). The shipped Qwen3.5 config overlay was
         tuned on gfx950 at 256 CUs; MI300A is gfx942 at 228 CUs (see
         hardware.md), so no overlay row ever matches and every dense
         projection silently takes the untuned path.
Fix:     re-tune locally for this (gfx, cu_num) with aiter's own tuner
         (set AITER_CONFIG_GEMM_BF16 to point at the regenerated config;
         the tuner needs --batch 1 to cover the decode shape). Tuning
         helps but does not fully close the gap: even a tuned kernel
         reaches only 3 to 10 percent of peak at M=16 for the skinniest
         shapes, so treat this as a partial mitigation, not a fix.
Scope:   rocm, gfx942, MI300A (228 CU), aiter d9e5ef7ce0.
Status:  verified (mechanism read from the lookup key, plus measured
         per-shape throughput). sglang-v0.5.18-rocm700-mi30x, 2026-09-11,
         job 631888.
```

### Cold dense-GEMM shape resolution is milliseconds, not the cause of multi-second stalls

```
Symptom: a burst of large, never-repeated prefill M values (session
         admission burst, unchunked prefill) coincides with multi-second
         server-side stalls, and the untuned tuned_gemm fallback (see the
         pitfall above) is a plausible suspect, since every fresh
         (M, N, K) shape is logged as "not found tuned config ... using
         torch solution".
Cause:   measured directly (six dense-projection shapes, 24 fresh M each
         from 301 to 2779, one device): a genuinely fresh shape costs 0.3
         to 7.7 ms for its first tuned_gemm.tgemm.mm call (a one-time 260
         to 290 ms outlier on the process's very first HIP launch only,
         not a per-shape cost, reproduced in two independent jobs).
         aiter's fallback for an unmatched shape is a direct, unmodified
         call into plain F.linear (`aiter/tuned_gemm.py`
         `solMap["torch"]`); aiter's own table lookup costs 85 us and
         forcing it to a no-op changes nothing. A 12-fresh-M x 6-GEMM
         admission burst sums to about 0.4 s total, roughly two orders of
         magnitude too small to be a single 5-10 s stall.
Fix:     rule this mechanism out before chasing it further; the actual
         cause of multi-second stalls on this stack is still open (see
         `tooling/serving-benchmark.md` and
         `algorithms/chunked-prefill.md` for the structural mitigation).
         Do not try to fix stalls by rounding M to a bucket: M=1021 and
         M=1024 both pay the same cold cost, and M=1277 pays more than
         M=1280, because hipBLASLt/rocBLAS keys its algorithm-search
         cache on exact M, not a bucket, a nearby unaligned M does not
         reuse a bucketed neighbor's warmed selection.
Scope:   gfx942, aiter d9e5ef7ce0, hipBLASLt in rocm 7.0.
Status:  verified (measured, reproduced in two jobs). sglang-v0.5.18-rocm700-mi30x, 2026-09-11, jobs 632902, 632911.
```

## Out of scope: kernel implementation

Writing new CDNA kernels (HIP, CK templates) is outside this collection. This file covers consuming existing libraries.

## See also

- [`floor.md`](floor.md): where the fused kernel sits in the optimization floor, and the validated launch recipe
- [`hardware.md`](hardware.md): CDNA3/CDNA4 precision support and GFX IDs
- [`unified-memory.md`](unified-memory.md): the mem_fraction_static x0.85 multiplier this library applies, and its consequence for save-time memory math
- [`weight-loading.md`](weight-loading.md): checkpoint materialization, independent of which attention/MoE kernel is selected
- [`boot-costs.md`](boot-costs.md): one-time process-lifetime costs (Gated DeltaNet autotune, this fork's custom HIP extension builds) to absorb in warmup, not measurement
- [`frameworks/triton.md`](../../frameworks/triton.md): the portable fallback
