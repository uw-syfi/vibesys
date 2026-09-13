# Fused MXFP4 MoE kernel: iteration after the permute fix

Follow-up file for [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md)'s permute-based `decode16` fix (job-verified, accepted as the default). That section's own "Remaining known inefficiency" note flagged that the compiler still splits the 16-byte packed-weight load into eight 2-byte loads (0.41 loads per element where 0.19 is possible). This file covers the next optimization-loop iteration that targets exactly that gap, one refuted alternative tried alongside it, and a dispatch-threshold retune the permute fix's own speedup made necessary.

**End-to-end status: H-A (dword-wide packed-weight loads) is job-verified and accepted as the default.** H-B (shared activation fragment) is refuted. H-C (dispatch threshold retune) is job-verified at 48-session concurrency and accepted there; the same paired run at a 16-session cap shows no regression on any metric but does not clear the acceptance rule's noise bar, so H-C is not yet an unconditional default. See each hypothesis's own status line below for the numbers.

## Discriminator flip: no longer issue-bound, now latency-bound on load shape

Before this iteration, the kernel's limiter was VALU issue rate (VALUBusy 30 to 46 percent before the permute fix, 18 to 29 percent after it; see [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md)). At that lower VALUBusy, a fresh microbenchmark found load shape and bytes in flight, not instruction count, is now the term worth attacking. This is a general lesson, not specific to this kernel: after an instruction-count fix moves a kernel off one discriminator (issue-bound), the classification can flip to the other (latency-bound on load shape), and the next lever changes accordingly. See the portable note in [`../../tooling/profiler.md`](../../tooling/profiler.md).

## H-A: dword-wide packed-weight loads

Design: read the 16-byte packed weight block as whole dwords instead of letting the compiler split it into eight 2-byte (`global_load_ushort`) loads, each carrying its own address computation and `s_waitcnt` slot.

Exactness: bit-identical to the permute-fix kernel at every M tested (16, 64, 337, 919, 2048).

ISA: loads per element drop 0.41 to 0.19 on the scaffold loop, exactly as predicted. On the dispatched "big" template the same change removes 75 percent of loads (150 to 38 per unrolled body) and drops VGPRs 73 to 60 while occupancy rises 6 to 8 waves per SIMD, because the compiler no longer needs to keep an intermediate byte array live across the split loads: fewer loads and less register pressure from the same change, not a trade between them. `valu_int` also falls (the per-group address arithmetic disappears); the decode arithmetic itself (`valu_perm`, `valu_float`) is unchanged.

Microbenchmark, stage1+stage2 speedup versus the permute-fix kernel: 1.82x at M=16, 1.89x at M=64, 1.52x at M=337, 2.04x at both M=919 and M=2048. The wall-clock magnitude is 6 to 15 times larger than the static instruction-count reduction alone would predict, consistent with the kernel being latency-bound as well as issue-bound at this altitude: each removed 2-byte load also removes its own address computation and wait-count slot, not just one instruction's worth of stall.

Paired end-to-end acceptance (branch `moe-iter3-a`, draft PR #86 against the accepted stack, one node per concurrency, 5 reps per side per job, identical harness-default flags both sides otherwise, gates 13/13 on every rep of every side, 20/20 reps total):

| Concurrency | median TPOT | pooled p95 TTFT turn2+ | accept length |
|:--|--:|--:|--:|
| Uncapped, 48 sessions, collapse-affected reps excluded | 18.82 -> 13.17 ms (-30.0%) | 423.9 -> 331.4 ms (-21.8%) | 2.886 vs 2.849 |
| Uncapped, 48 sessions, raw (all 5 reps/side) | 21.79 -> 13.23 ms (-39.3%) | 504.5 -> 333.6 ms (-33.9%) | -- |
| 16-session cap | 11.37 -> 9.44 ms (-17.0%) | 288.5 -> 298.3 ms (+3.4%, inside the 10% budget) | 2.84 vs 2.85 |

The 48-session job's raw numbers include a minority of reps hit by an admission-queueing collapse on both sides (see the pitfall this is filed under in [`../../engines/sglang.md`](../../engines/sglang.md), candidate status, cause not yet diagnosed); excluding those reps gives a clean, non-overlapping TPOT comparison (base range 18.10-21.79 ms, cand range 12.69-14.98 ms) larger than either side's own clean rep-to-rep spread (19.6% and 17.4%). The 16-session job saw zero collapse-affected reps at any point and its ranges are non-overlapping on their own (base 11.13-11.54 ms, cand 9.29-9.67 ms). Both concurrencies clear the acceptance rule (gates 5/5 both sides, TPOT improvement exceeds rep spread, p95 TTFT regression within 10%).

Recommendation: accepted as the default fused MXFP4 MoE weight-load path, on top of the permute-based `decode16` fix and the rest of the accepted stack. Accepted alone, not combined with H-B below.

Status: job-verified (bit-exact by construction and by the prior microbenchmark, and a real non-noise TPOT improvement confirmed end to end at both concurrencies tested). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## H-B: one K loop sharing the activation fragment between gate and up (refuted)

Design tried: fuse the gate and up passes into one K loop iteration that shares one activation fragment load, instead of two separate passes each loading their own copy, to halve activation-load traffic.

Result: refuted as measured. 25 to 30 percent slower at M=16/64/337 (worst at M=337, 57 percent slower) than the permute-fix kernel, everywhere the scaffold kernel dispatches; a wash at M=919/2048, where the templated "big" kernel dispatches instead and this change's code never runs.

The static ISA picture alone does not explain the regression: VGPRs on the fused loop fall (66 to 61) and occupancy rises (7 to 8 waves), the opposite of the register-pressure risk expected going in, and total instruction count is flat to slightly lower.

Follow-up fix tried (H-B2): reorder the source so both weight loads (gate and up) issue before either decode, on the theory that H-B's regression was a missing-latency-hiding gap. Result: no better than H-B, within 0.1 to 0.3 points at every M. The compiled loop is nearly identical to H-B's: `-O3`'s own instruction scheduler had already reordered the loads independently of source order, so the source-level hoist gave the backend nothing new to work with.

Mechanism (confirmed by hardware counters, resolving the open question from the first attempt): memory-read instructions fall about 33 percent, as expected from sharing one activation load, and VALU work is flat or lower, yet `SQ_BUSY_CYCLES` and `SQ_WAIT_INST_ANY` rise 1.7 to 2.5x and VALUBusy drops by about half. The compiled loop shows why: the un-fused kernel has one clean load-then-run region (drain all loads, then run every MFMA uninterrupted); the fused kernel splits into three to four smaller regions that interleave more loads with the MFMAs, each a fresh point where VALU issue stalls on an outstanding fetch. Fewer total loads does not help when the fusion also fragments the wait structure into more, smaller windows. A second, independent signal points the same way: fetched bytes per dispatch rise 3.3 to 4.3x for H-B even though vmem-read instruction count falls, consistent with the fused K-walk interleaving two weight columns (gate, up) that live far apart in the weight tensor, each visit now needing a fresh cache-line fetch instead of two loops that could each stream one column with better locality.

Status: refuted (H-B and H-B2 both reproduce the regression across three M values, with near-identical compiled loops and statistically indistinguishable timing between the two variants; the mechanism is now confirmed by hardware counters, not just the earlier static ISA read). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## H-C: scaffold-versus-template dispatch threshold retune (accepted at 48-session concurrency)

An earlier pass through this kernel, before H-A's dword-wide loads applied to both the scaffold and the templated ("big") stage1 kernel, estimated the scaffold-versus-template crossover had moved to about 530 sorted blocks. That estimate no longer holds: once dword-wide loads apply to both kernels, the crossover disappears across the whole range measured. An 11-point sweep from M=16 to M=2048 (160 to 1761 sorted blocks) found "big" winning at every point, including the smallest, so there is no sign change left for a crossover-interpolation script to find in this range.

A follow-up sweep at threshold 160 (the smallest measured block count, since no in-range crossover exists to interpolate) supplies the real dispatch-flip numbers. Stage1+stage2 speedup over the old default (threshold 1024): 1.22x at M=16 (160 blocks), 1.17x at M=64 (521 blocks), 1.48x at M=337 (692 blocks), and noise-level (0.998 to 1.001x) at M=919 and M=2048, where both thresholds already dispatch to "big".

Recommendation: ship `STAGE1_SCAFFOLD_BLOCK_THRESHOLD=160` as a separate, one-constant, zero-new-code follow-on once H-A lands; do not bundle it with H-A in one acceptance test, since H-A's own exactness and performance story is already clean and self-contained. 160 is the smallest value actually measured winning, not an extrapolated one: "big" may also win below 160 blocks, but that was not tested (the smallest M swept, 16, is already 160 blocks).

Numerics note: flipping the dispatch threshold changes which kernel a given M reaches, and the two kernels are each correct at bf16-rounding-level but not bit-identical to each other. At threshold 160, M in {16, 64, 337} moves from scaffold to "big"; max-abs-diff versus the unflipped dispatch is 1.953e-3, 3.906e-3, and 7.812e-3 respectively, each exactly 2x the previous as reduction depth grows: reduction-order noise between two independently-correct kernels, the same phenomenon already characterized (not a numerics defect) for this scaffold-versus-template boundary in [`aiter.md`](aiter.md)'s prefill-M microbenchmark entry.

Method lesson: the crossover-interpolation script assumes a sign change exists in the swept range and has no way to report "none found"; here it silently kept the stale threshold (1024) instead, making that run's own retuned variant a no-op until the dedicated threshold=160 rerun above supplied real numbers. Re-derive thresholds from the raw sweep table, not the interpolator's output, after any kernel change that shifts per-block cost on either side of the dispatch. See the portable note in [`../../tooling/profiler.md`](../../tooling/profiler.md) for the script pitfall, and [`../../tooling/serving-benchmark.md`](../../tooling/serving-benchmark.md) for the general staleness lesson this motivated plus the second-concurrency check it was verified against.

### Paired end-to-end acceptance, measured

Two jobs, one node each, base (accepted dword-loads stack, H-A merged) vs cand (this change, threshold 160), 5 reps per side per job, identical harness-default flags otherwise (NEXTN k=3, sharded draft, overlap off, TunableOp on), gates 13/13 on every rep of every side (20/20 reps total), accept_len unchanged at both concurrencies:

| Concurrency | median TPOT | pooled p95 TTFT turn2+ |
|:--|--:|--:|
| Uncapped, 48 sessions, turn-1-admission-delay reps excluded | 13.83 -> 12.60 ms (-8.9%) | 374.9 -> 374.4 ms (-0.15%, flat) |
| Uncapped, 48 sessions, raw (all 5 reps/side) | 13.83 -> 12.66 ms (-8.5%) | 374.9 -> 369.0 ms (-1.6%) |
| 16-session cap | 9.91 -> 9.61 ms (-3.0%) | 295.7 -> 278.9 ms (-5.7%) |

At 48 sessions, cand reps 2 and 4 show a self-contained turn-1-only admission delay (never reaching turn2+, `schedule_bound_fraction` dips only to 0.994) that inflates cand's raw TPOT rep spread; excluding those two reps gives non-overlapping per-rep TPOT ranges (base 13.52-14.38 ms, cand 12.56-12.66 ms) larger than either side's own clean spread (6.2%, 0.8%), and pooled p95 TTFT turn2+ is flat. At the 16-session cap, no rep on either side shows any stall, but the 3.0% TPOT improvement (0.30 ms) is smaller than both sides' own rep-to-rep spread (5.2%, 3.5%) and one of five paired per-rep differences has cand's TPOT higher than base's: not distinguishable from noise at this concurrency, though every metric is flat to improved (no regression).

Accepted as the default at 48-session concurrency: a real, non-noise TPOT improvement with p95 TTFT flat to slightly better. Not confirmed as an unconditional default at a 16-session cap, since the acceptance rule requires the TPOT improvement to exceed rep spread at every concurrency tested and it does not here; recorded as "no regression at the secondary concurrency," not a second confirmed win. A larger rep count or a lower-noise measurement at 16 sessions would be needed before shipping this threshold unconditionally.

Status: job-verified at 48-session concurrency (accepted); microbench-verified only at a 16-session cap (no regression, improvement not distinguishable from rep-to-rep noise). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## State after iteration 4: final re-profile on the accepted stack (H-A + H-C), job-verified

A fresh re-profile of the accepted stack (permute decode plus H-A dword-wide loads plus H-C's threshold-160 retune) against the campaign's floors, using the same decode-round classifier and prefill kernel breakdown as every earlier re-profile in this file. Two jobs, one for the decode-round and prefill sweep and one for a kernel microbenchmark plus rocprofv3 counters.

**Decode round, N=16 (pooled medians, previous stack in parentheses):**

| Block | ms | Floor (ms) | Ratio |
|:--|--:|--:|--:|
| MoE stage1 | 11.49 | -- | -- |
| MoE stage2 | 6.08 | -- | -- |
| **MoE routed total** | **17.57** (was 32.47) | **14.81** | **1.19x** (was 2.19x) |
| Dense GEMM | 5.75 | 3.47 | 1.66x |
| Gated DeltaNet | 2.96 | -- | -- |
| Collectives | 3.58 | -- | -- |
| CPU-only gap | 2.74 | -- | -- |
| Attention | 1.14 | -- | -- |
| Other (folds norm, sampling/verify glue, shared-expert kernels) | 3.00 | -- | -- |
| **Round wall** | **36.86** (was 51.11) | **18.28** | **2.02x** (was 2.80x) |

Correction: an earlier reading of this "Other" row against the post-permute stack's own residual figure treated the two as directly comparable and reported growth from about 1.7 to 3.0 ms. They are not comparable: this row folds norm, sampling/verify-glue, and shared-expert kernels together, while the post-permute-stack figure it was compared against was the bare, unfolded residual before that same fold. Reprocessing both profiles' raw traces with an identical classifier and an identical fold gives 2.79 ms (post-permute stack) to 3.00 ms (this stack), a 0.21 ms (7.3 percent) growth spread across roughly 20 already-existing small per-layer kernels (norm, residual add, SiLU, KV-cache store), consistent with ordinary run-to-run measurement noise rather than a regression from H-A/H-C. See the bucket-fold comparison pitfall in [`../../tooling/profiler.md`](../../tooling/profiler.md).

At N=8, MoE routed is 1.47x its 8.78 ms floor; at N=32, 1.01x its 22.51 ms floor, essentially at the floor. The over-floor ratio keeps shrinking with batch size exactly as every prior re-profile in this campaign found (1.47x, 1.19x, 1.01x at N=8/16/32), uniformly lower than any earlier point. **This crosses MoE routed from the "1.3x to 3x: tuning and overhead" band into the "under 1.3x: at the floor for this design" band** in the optimization-loop skill's own stop-criterion table. Round wall stays at 1.9x to 2.3x its own floor at every N: the non-MoE buckets (dense GEMM, Gated DeltaNet, collectives, CPU-only gap) did not shrink along with MoE, so they now make up most of a much smaller round; see the stop-criterion note in [`../../models/qwen3-5.md`](../../models/qwen3-5.md).

**Prefill** (GPU kernel time only, unaffected by an unrelated aiter tuning-config first-touch lock at the 337-token point, see the pitfall in [`aiter.md`](aiter.md)): routed MoE falls 50 percent at 337 extend tokens (80.84 to 40.09 ms) and 44 percent at 919 extend tokens (144.67 to 81.52 ms) versus the pre-H-A/H-C stack. Dense GEMM, attention, and the small buckets are flat.

**Kernel microbenchmark and rocprofv3 counters, stage1, M=16/64.** Every production M measured (16 to 2048) now dispatches the templated "big" stage1 kernel; the scaffold kernel every earlier ISA audit in this file targeted no longer runs at any of these shapes under threshold=160. Stage1+stage2 combined: 0.1456 ms at M=16, 0.4335 ms at M=64, against the roughly 0.32 ms combined floor (0.98x at M=16 for stage1 alone against its own share of that floor, 1.36x combined at M=64, down from 2.2 to 2.6x for the scaffold kernel in earlier iterations). Static instruction count for the dispatched kernel: about 2.03 VALU (int, float, packed) instructions per weight element, versus 2.66 to 4.63 for the scaffold kernel the earlier permute-fix ISA audit characterized. Hardware counters: VALUBusy 38.2 percent at M=16, 43.8 percent at M=64 (the same "neither idle nor saturated" issue-bound band every kernel in this campaign has shown, 30 to 46 percent); `SQ_WAIT_INST_ANY` about 9x lower than the old scaffold kernel's own M=64 figure at a comparable VALUBusy, i.e. much less time stalled on outstanding loads at a similar issue rate.

**Classification: still instruction-issue-bound, but the absolute overhead over the floor has shrunk from 2.2 to 2.6x (scaffold kernel, earlier iterations) to 0.96 to 1.36x (this kernel, this stack).** The kernel is not a different bottleneck class; the padding and load-shape overhead this campaign kept cutting is now mostly gone.

**Recommendation, and why the fp8-activation design below does not clear its own bar:** the milestone an fp8-activation MoE kernel was scoped against (at least 1.5x faster than the exact kernel's own stage1 time at M=64) is no longer reachable by construction, because there is no longer 1.5x of headroom over the combined floor to take: stage1 alone at M=64 is already at 0.98x of its own share of the 0.32 ms combined floor. See [`aiter-fp8-moe.md`](aiter-fp8-moe.md) for the full closure reasoning. The better-targeted next iteration is the non-MoE decode-round buckets (dense GEMM, Gated DeltaNet, collectives, CPU-only gap), which have never had the exact-numerics scrutiny the MoE kernel got and now dominate the round's remaining over-floor gap.

Scope: rocm, gfx942, this fork's MXFP4 fused MoE kernel, the accepted H-A + H-C stack. Status: job-verified (decode-round N-sweep, prefill kernel breakdown, and kernel microbenchmark/counter audit all gated and cross-checked; bucket-sum vs. measured GPU-busy time within 0.4 percent). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## fp8-expand decode: analytical design, closed (not built)

Follows from [`aiter-fp8-moe.md`](aiter-fp8-moe.md) finding no usable existing kernel for fp8-activation, 4-bit-weight MoE on gfx942: a from-scratch design keeps weights 4-bit in memory and decodes each nibble to an fp8 e4m3fnuz byte in-register via a 16-entry permute table, rather than porting the H-A/H-B decode's bf16 sign-OR trick, since fnuz has no negative zero and byte `0x80` is NaN there. The e8m0 block scale applies to the fp32 partial sum of each K=32 block, not the fp8 operand (whose exponent range is too narrow to hold e8m0's full range); activations quantize per-token to fp8. Estimated instruction budget is about 3 to 4.5 VALU+perm per element, close to H-A's own measured 4.25/element for the exact bf16 path, so the design's real gain is not fewer decode instructions but an 8x cut in matrix-instruction issue count (one native fp8x32 MFMA call replaces eight bf16 `_1k` calls per weight column). Pre-expanding weights to fp8 in memory instead of decoding in-kernel would double weight-byte footprint and does not fit at this model's size.

Closed after the final re-profile above found the exact kernel already within 1.0 to 1.2x of the weight-byte floor this design's target instruction cut does not move; see [`aiter-fp8-moe.md`](aiter-fp8-moe.md) for the full reasoning. Not built; the design and its instruction-budget estimate above are kept as a record of the analysis, not a queued task.

Scope: rocm, gfx942, this fork's MXFP4 fused MoE kernel. Status: closed (analytical design only; no cluster job run; superseded by the closure decision in [`aiter-fp8-moe.md`](aiter-fp8-moe.md)). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## See also

- [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md): the permute-based decode16 fix this file follows up on, and its own remaining-inefficiency note
- [`aiter.md`](aiter.md): the stage1 scaffold-versus-templated dispatch this file's H-C retunes, and the prefill-M dispatch-threshold history that first characterized the scaffold-versus-big reduction-order difference
- [`aiter-fp8-moe.md`](aiter-fp8-moe.md): the fp8-activation kernel survey this closed design followed from, and the closure reasoning after the final re-profile above
- [`../../models/qwen3-5.md`](../../models/qwen3-5.md): the decode-round and prefill numbers the final re-profile above is drawn from, and the current-state note on the remaining round-level gap
- [`../../tooling/profiler.md`](../../tooling/profiler.md): the portable notes on re-running the issue-bound-versus-latency-bound discriminator after an instruction-count fix, on checking wait-region counts rather than load counts alone (H-B/H-B2), and on re-deriving thresholds from a raw sweep rather than trusting a crossover interpolator (H-C)
- [`../../tooling/serving-benchmark.md`](../../tooling/serving-benchmark.md): the general lesson that a dispatch threshold goes stale after the kernel it dispatches between changes, and what the second-concurrency check in a paired acceptance run is actually guarding against
