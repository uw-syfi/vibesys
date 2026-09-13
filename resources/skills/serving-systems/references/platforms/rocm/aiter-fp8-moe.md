# fp8-activation MoE kernels for MXFP4 weights on gfx942

Which aiter kernel families pair an fp8-activation quantization with a 4-bit weight, and which of them actually run on gfx942, for a checkpoint whose native scheme is MXFP4 weight + MXFP4 activation.

Follow-up detail for [`aiter.md`](aiter.md)'s MXFP4 capability table: that table covers the native MXFP4-weight path (`QuarkW4A4MXFp4MoE`), which never reaches an aiter kernel on gfx942 at all. This file covers the separate question of whether any *other* wired kernel could give this checkpoint fp8-activation, 4-bit-weight MoE on gfx942.

## The checkpoint's own scheme falls back to bf16 activations on gfx942

Qwen3.5's checkpoint declares `QuarkW4A4MXFp4MoE` (`quark_w4a4_mxfp4_moe.py`): weight and activation both per-group MXFP4, via aiter's `dynamic_mxfp4_quant`. This never reaches an aiter kernel on gfx942 (see [`aiter.md`](aiter.md)'s capability table); it falls back to the fork's own permute-decode kernel or the Triton `fused_moe_kernel_gptq_awq` w4a16 path, both bf16 activation.

A second in-tree scheme, `QuarkW4A8MXFp4MoE` ("MXFP4 weights with static FP8 activations"), is the literal fp8-activation-x-MXFP4-weight scheme SGLang carries, but it (a) raises `NotImplementedError` unless the AITER MoE-runner backend is selected, and (b) even then calls aiter's `fused_moe(quant_type=QuantType.per_1x32, ...)`, whose own operand-dtype selection picks bf16 whenever `get_gfx() != "gfx950"`, regardless of the checkpoint's declared static fp8 scale. On gfx942 this always resolves to bf16. Neither in-tree scheme reaches fp8 activations on this platform.

## Compatibility: fp8-activation x 4-bit-weight kernel families

| Kernel family | Weight | Activation | gfx942? | Evidence |
|:--|:--|:--|:--|:--|
| CK 2-stage "wint4" (`ck_moe_stage1_fwd`/`ck_moe_stage2_fwd`, `quant_type=per_Token`) | linear int4, per-group scale | fp8, dynamic per-token | Runs, but output is NaN (below) | codegen forces `a_dtype=f8` whenever `b_dtype=i4`; no gfx942 exclusion, unlike the fp4x2 rows below |
| Triton a8w4 (`aiter.ops.triton.moe.moe_op_gemm_a8w4`) | int4 | fp8 | No: fails to import | needs triton >= 3.6.0; this image ships 3.4.0. No gfx942 tuned config ships either way (`gfx950-A8W4.json` only) |
| ASM/FlyDSL "a8w4" (`kimik3_a8w4_*_fmoe.csv`) | MXFP4 (fp4x2), not int4 | fp8, via SiTUv2-activation kernels | No | every config row is `gfx950`; also requires `ActivationType.Situv2`, not this model's SiLU/Swiglu |
| Opus a8w4 (`aiter/ops/opus/moe_stage2_a8w4.py`) | int4 | fp8 | No | its only kernel contract is named for gfx950 explicitly |
| aiter native MXFP4 (`per_1x32`, fp4x2 weight) | MXFP4 | bf16 always on gfx942 (fp8 only on gfx950 at M >= 256) | No: aborts | see pitfall below |

Only the CK "wint4" family both runs on gfx942 and pairs fp8 activation with a 4-bit weight; it is also the fastest candidate measured (0.175-0.26 ms/layer at M=16-919, 62-92 percent of the weight-bandwidth floor, versus 0.95-2.4 ms for the exact fused kernel at the same M) but its output is wrong (below). The other families are real fp8-activation kernels that pair with a different weight format (MXFP4/fp4x2) or are gfx950-only by construction; none is a live-testable gap on gfx942.

Weight bytes per layer are identical between the two formats by construction, not coincidence: MXFP4 spends 1 byte (e8m0) per 32-element block, and this survey's linear-int4 spends 4 bytes (fp32) per 128-element group; both work out to 1/32 byte of scale overhead per element.

Even a correct CK wint4 kernel would still need a real MXFP4-to-int4 requantization of the checkpoint: MXFP4's 16 non-uniform e2m1 levels are not a bit-reinterpretation of int4's uniform linear levels, so that conversion is a separate, independently riskable accuracy question, not resolved by fixing the NaN below.

### CK wint4 kernel runs but returns NaN

```
Symptom: ck_moe_stage1_fwd/ck_moe_stage2_fwd (quant_type=per_Token, fp8
         activation x int4 weight) return without raising, with plausible
         per-layer wall time that scales with M, but every output element
         is NaN at every M tested (16, 64, 337, 919).
Cause:   not isolated. Two plausible causes were checked and fixed
         without resolving it: (1) stage1's output buffer was first sized
         at 2x inter_size, assuming an unreduced gate_up intermediate;
         ck_moe_stage1_fwd actually fuses silu(gate)*up into its own
         epilogue and returns inter_size width, so this only produced a
         downstream reshape error, fixed by allocating inter_size-wide.
         (2) the per-token fp8 quantization step clamped to +/-448 (the
         OCP e4m3fn max), but ROCm's fp8 is float8_e4m3fnuz, whose max
         magnitude is 240; values in (240, 448] would overflow to inf on
         cast and poison the kernel's scale-multiply epilogue with NaN.
         Fixed to query torch.finfo(dtypes.fp8).max instead; NaN
         persisted unchanged. Most likely remaining candidates: the
         physical layout the kernel expects for w1_scale/w2_scale after
         its own int4 packing/interleave convention, which this survey's
         straightforward group-128 scale tensor may not match.
Fix:     not found this round. Worth one focused follow-up (check the
         scale-tensor layout convention against aiter's own int4 packing
         test path) before committing to a from-scratch kernel, given the
         speedup would clear this campaign's 20 percent bar by 2x-9x if
         fixed.
Scope:   rocm, gfx942, aiter d9e5ef7ce0, this image.
Status:  candidate (root cause not isolated; runs and times plausibly,
         output unusable). Stamp: sglang-v0.5.18-rocm700-mi30x,
         2026-09-13, job-verified.
```

### aiter's native MXFP4 path aborts on gfx942, not just falls back

```
Symptom: calling aiter's native fused_moe(quant_type=QuantType.per_1x32)
         with real MXFP4 (float4_e2m1fn_x2) weights hard-aborts the
         process (not a catchable Python exception); log shows
         "fused_dynamic_mx_quant_moe_sort_hip: not support output type:
         fp4x2" (quant_kernels.cu:1987), then a core dump.
Cause:   the fused quant+sort kernel that would decide activation dtype
         (bf16 vs fp8, see aiter.md's capability table) never runs on
         gfx942 at all; it aborts before reaching that decision. This is
         the same abort aiter.md's capability table already documents,
         reproduced live here specifically to confirm no fp8-activation
         decision point exists on this path on gfx942, not merely that
         it defaults to bf16.
Fix:     none on gfx942; this path targets gfx950. Do not attempt to
         force this path with a bf16-weight or padded-fp4x2 workaround
         without first checking whether the target op even compiles for
         gfx942 (it does not, per aiter.md's capability table).
Scope:   rocm, gfx942, aiter d9e5ef7ce0.
Status:  verified (reproduced live; matches aiter.md's prior source-read
         finding). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
         job-verified.
```

## Recommendation

No existing kernel gives this checkpoint fp8-activation, 4-bit-weight MoE on gfx942 today. Fixing the CK wint4 NaN is the cheapest lever if pursued (candidate, above); absent that, the next-cheapest lever is a custom in-kernel fp8-expand decode, tracked as a candidate design in [`aiter-mxfp4-moe-kernel-iterations.md`](aiter-mxfp4-moe-kernel-iterations.md).

## Out of scope: kernel implementation

Writing new CDNA kernels (HIP, CK templates) is outside this collection. This file covers consuming existing libraries.

## See also

- [`aiter.md`](aiter.md): the MXFP4 capability table and gate mechanism this file's kernel families are checked against
- [`aiter-mxfp4-moe-kernel-iterations.md`](aiter-mxfp4-moe-kernel-iterations.md): the fp8-expand candidate design that follows from this survey finding no usable existing kernel
- [`../../algorithms/quantization-schemes.md`](../../algorithms/quantization-schemes.md): the portable N/A row for this finding
