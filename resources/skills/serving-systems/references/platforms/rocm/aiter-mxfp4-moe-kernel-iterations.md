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

The static ISA picture does not explain the regression, which makes it the interesting result: VGPRs on the fused loop fall (66 to 61) and occupancy rises (7 to 8 waves), the opposite of the register-pressure risk expected going in, and total instruction count is flat to slightly lower. No `VALUBusy` or `SQ_WAIT_INST_ANY` hardware counters were collected for this variant, so the cause is not isolated; the working explanation is reduced instruction-level parallelism from interleaving two independent decode-and-accumulate chains into one longer basic block per iteration (worse load and compute overlap across iterations), but this is unconfirmed and flagged as a follow-up.

Status: refuted (reproducible regression across three M values and both single and combined builds; every static ISA signal pointed the other way; the mechanism needs a dynamic profile not yet attempted). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## H-C: scaffold-versus-template dispatch threshold retune

The scaffold-versus-templated crossover this kernel dispatches on moved from about 740 to about 530 sorted blocks once the permute fix made decode cheaper: "big"'s extra per-block fixed cost now amortizes faster relative to the scaffold's own per-block overhead than it did against the old, decode-heavier baseline. That is a 29 percent move, past this campaign's own 20 percent bar for proposing a new constant.

Recommendation: ship the new threshold as a separate, one-constant, zero-new-code follow-on once H-A lands; do not bundle it with H-A in one acceptance test, since H-A's own exactness and performance story is already clean and self-contained.

Numerics note: flipping the dispatch threshold changes which kernel a given M reaches, and the two kernels are each correct at bf16-rounding-level but not bit-identical to each other. At M=337, which the new threshold moves from scaffold to "big", max-abs-diff versus the unflipped dispatch is 3.9e-3: reduction-order noise between two independently-correct kernels, the same phenomenon already characterized (not a numerics defect) for this scaffold-versus-template boundary in [`aiter.md`](aiter.md)'s prefill-M microbenchmark entry.

Status: microbench-verified (crossover measured directly by an 11-point sweep from M=16 to M=2048; not yet shipped). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## See also

- [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md): the permute-based decode16 fix this file follows up on, and its own remaining-inefficiency note
- [`aiter.md`](aiter.md): the stage1 scaffold-versus-templated dispatch this file's H-C retunes, and the prefill-M dispatch-threshold history that first characterized the scaffold-versus-big reduction-order difference
- [`../../tooling/profiler.md`](../../tooling/profiler.md): the portable note on re-running the issue-bound-versus-latency-bound discriminator after an instruction-count fix
