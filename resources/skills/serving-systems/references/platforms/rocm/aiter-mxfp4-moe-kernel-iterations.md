# Fused MXFP4 MoE kernel: iteration after the permute fix

Follow-up file for [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md)'s permute-based `decode16` fix (job-verified, accepted as the default). That section's own "Remaining known inefficiency" note flagged that the compiler still splits the 16-byte packed-weight load into eight 2-byte loads (0.41 loads per element where 0.19 is possible). This file covers the next optimization-loop iteration that targets exactly that gap, one refuted alternative tried alongside it, and a dispatch-threshold retune the permute fix's own speedup made necessary.

**End-to-end status: acceptance in progress.** Everything below is a kernel microbenchmark and ISA-level result, not a paired multi-turn serving measurement; no TTFT or TPOT numbers are claimed here.

## Discriminator flip: no longer issue-bound, now latency-bound on load shape

Before this iteration, the kernel's limiter was VALU issue rate (VALUBusy 30 to 46 percent before the permute fix, 18 to 29 percent after it; see [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md)). At that lower VALUBusy, a fresh microbenchmark found load shape and bytes in flight, not instruction count, is now the term worth attacking. This is a general lesson, not specific to this kernel: after an instruction-count fix moves a kernel off one discriminator (issue-bound), the classification can flip to the other (latency-bound on load shape), and the next lever changes accordingly. See the portable note in [`../../tooling/profiler.md`](../../tooling/profiler.md).

## H-A: dword-wide packed-weight loads

Design: read the 16-byte packed weight block as whole dwords instead of letting the compiler split it into eight 2-byte (`global_load_ushort`) loads, each carrying its own address computation and `s_waitcnt` slot.

Exactness: bit-identical to the permute-fix kernel at every M tested (16, 64, 337, 919, 2048).

ISA: loads per element drop 0.41 to 0.19 on the scaffold loop, exactly as predicted. On the dispatched "big" template the same change removes 75 percent of loads (150 to 38 per unrolled body) and drops VGPRs 73 to 60 while occupancy rises 6 to 8 waves per SIMD, because the compiler no longer needs to keep an intermediate byte array live across the split loads: fewer loads and less register pressure from the same change, not a trade between them. `valu_int` also falls (the per-group address arithmetic disappears); the decode arithmetic itself (`valu_perm`, `valu_float`) is unchanged.

Microbenchmark, stage1+stage2 speedup versus the permute-fix kernel: 1.82x at M=16, 1.89x at M=64, 1.52x at M=337, 2.04x at both M=919 and M=2048. The wall-clock magnitude is 6 to 15 times larger than the static instruction-count reduction alone would predict, consistent with the kernel being latency-bound as well as issue-bound at this altitude: each removed 2-byte load also removes its own address computation and wait-count slot, not just one instruction's worth of stall.

Recommendation: promoted to a paired end-to-end acceptance test (in progress). Recommended alone, not combined with H-B below.

Status: microbench-verified (exactness check, ISA cross-check, and microbenchmark all confirm the mechanism; end-to-end acceptance not yet run). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## H-B: one K loop sharing the activation fragment between gate and up (refuted)

Design tried: fuse the gate and up passes into one K loop iteration that shares one activation fragment load, instead of two separate passes each loading their own copy, to halve activation-load traffic.

Result: refuted as measured. 25 to 30 percent slower at M=16/64/337 (worst at M=337, 57 percent slower) than the permute-fix kernel, everywhere the scaffold kernel dispatches; a wash at M=919/2048, where the templated "big" kernel dispatches instead and this change's code never runs.

The static ISA picture alone does not explain the regression: VGPRs on the fused loop fall (66 to 61) and occupancy rises (7 to 8 waves), the opposite of the register-pressure risk expected going in, and total instruction count is flat to slightly lower.

Follow-up fix tried (H-B2): reorder the source so both weight loads (gate and up) issue before either decode, on the theory that H-B's regression was a missing-latency-hiding gap. Result: no better than H-B, within 0.1 to 0.3 points at every M. The compiled loop is nearly identical to H-B's: `-O3`'s own instruction scheduler had already reordered the loads independently of source order, so the source-level hoist gave the backend nothing new to work with.

Mechanism (confirmed by hardware counters, resolving the open question from the first attempt): memory-read instructions fall about 33 percent, as expected from sharing one activation load, and VALU work is flat or lower, yet `SQ_BUSY_CYCLES` and `SQ_WAIT_INST_ANY` rise 1.7 to 2.5x and VALUBusy drops by about half. The compiled loop shows why: the un-fused kernel has one clean load-then-run region (drain all loads, then run every MFMA uninterrupted); the fused kernel splits into three to four smaller regions that interleave more loads with the MFMAs, each a fresh point where VALU issue stalls on an outstanding fetch. Fewer total loads does not help when the fusion also fragments the wait structure into more, smaller windows. A second, independent signal points the same way: fetched bytes per dispatch rise 3.3 to 4.3x for H-B even though vmem-read instruction count falls, consistent with the fused K-walk interleaving two weight columns (gate, up) that live far apart in the weight tensor, each visit now needing a fresh cache-line fetch instead of two loops that could each stream one column with better locality.

Status: refuted (H-B and H-B2 both reproduce the regression across three M values, with near-identical compiled loops and statistically indistinguishable timing between the two variants; the mechanism is now confirmed by hardware counters, not just the earlier static ISA read). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## H-C: scaffold-versus-template dispatch threshold retune

An earlier pass through this kernel, before H-A's dword-wide loads applied to both the scaffold and the templated ("big") stage1 kernel, estimated the scaffold-versus-template crossover had moved to about 530 sorted blocks. That estimate no longer holds: once dword-wide loads apply to both kernels, the crossover disappears across the whole range measured. An 11-point sweep from M=16 to M=2048 (160 to 1761 sorted blocks) found "big" winning at every point, including the smallest, so there is no sign change left for a crossover-interpolation script to find in this range.

A follow-up sweep at threshold 160 (the smallest measured block count, since no in-range crossover exists to interpolate) supplies the real dispatch-flip numbers. Stage1+stage2 speedup over the old default (threshold 1024): 1.22x at M=16 (160 blocks), 1.17x at M=64 (521 blocks), 1.48x at M=337 (692 blocks), and noise-level (0.998 to 1.001x) at M=919 and M=2048, where both thresholds already dispatch to "big".

Recommendation: ship `STAGE1_SCAFFOLD_BLOCK_THRESHOLD=160` as a separate, one-constant, zero-new-code follow-on once H-A lands; do not bundle it with H-A in one acceptance test, since H-A's own exactness and performance story is already clean and self-contained. 160 is the smallest value actually measured winning, not an extrapolated one: "big" may also win below 160 blocks, but that was not tested (the smallest M swept, 16, is already 160 blocks).

Numerics note: flipping the dispatch threshold changes which kernel a given M reaches, and the two kernels are each correct at bf16-rounding-level but not bit-identical to each other. At threshold 160, M in {16, 64, 337} moves from scaffold to "big"; max-abs-diff versus the unflipped dispatch is 1.953e-3, 3.906e-3, and 7.812e-3 respectively, each exactly 2x the previous as reduction depth grows: reduction-order noise between two independently-correct kernels, the same phenomenon already characterized (not a numerics defect) for this scaffold-versus-template boundary in [`aiter.md`](aiter.md)'s prefill-M microbenchmark entry.

Method lesson: the crossover-interpolation script assumes a sign change exists in the swept range and has no way to report "none found"; here it silently kept the stale threshold (1024) instead, making that run's own retuned variant a no-op until the dedicated threshold=160 rerun above supplied real numbers. Re-derive thresholds from the raw sweep table, not the interpolator's output, after any kernel change that shifts per-block cost on either side of the dispatch. See the portable note in [`../../tooling/profiler.md`](../../tooling/profiler.md).

Status: microbench-verified (crossover re-measured directly by an 11-point sweep from M=16 to M=2048, plus a dedicated threshold=160 rerun with real dispatch-flip and speedup numbers; not yet shipped). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## fp8-expand decode: analytical design, candidate

Follows from [`aiter-fp8-moe.md`](aiter-fp8-moe.md) finding no usable existing kernel for fp8-activation, 4-bit-weight MoE on gfx942: a from-scratch design keeps weights 4-bit in memory and decodes each nibble to an fp8 e4m3fnuz byte in-register via a 16-entry permute table, rather than porting the H-A/H-B decode's bf16 sign-OR trick, since fnuz has no negative zero and byte `0x80` is NaN there. The e8m0 block scale applies to the fp32 partial sum of each K=32 block, not the fp8 operand (whose exponent range is too narrow to hold e8m0's full range); activations quantize per-token to fp8. Estimated instruction budget is about 3 to 4.5 VALU+perm per element, close to H-A's own measured 4.25/element for the exact bf16 path, so the design's real gain is not fewer decode instructions but an 8x cut in matrix-instruction issue count (one native fp8x32 MFMA call replaces eight bf16 `_1k` calls per weight column). Pre-expanding weights to fp8 in memory instead of decoding in-kernel would double weight-byte footprint and does not fit at this model's size.

Scope: rocm, gfx942, this fork's MXFP4 fused MoE kernel. Status: candidate (analytical design only; no cluster job run). What would verify it: a stage1-only prototype at decode M=64 reaching at least 1.5x over this kernel's own stage1 time, with rel_l2 within the e4m3 expectation and no NaN/inf. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, candidate.

## See also

- [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md): the permute-based decode16 fix this file follows up on, and its own remaining-inefficiency note
- [`aiter.md`](aiter.md): the stage1 scaffold-versus-templated dispatch this file's H-C retunes, and the prefill-M dispatch-threshold history that first characterized the scaffold-versus-big reduction-order difference
- [`aiter-fp8-moe.md`](aiter-fp8-moe.md): the fp8-activation kernel survey this candidate design follows from
- [`../../tooling/profiler.md`](../../tooling/profiler.md): the portable notes on re-running the issue-bound-versus-latency-bound discriminator after an instruction-count fix, on checking wait-region counts rather than load counts alone (H-B/H-B2), and on re-deriving thresholds from a raw sweep rather than trusting a crossover interpolator (H-C)
