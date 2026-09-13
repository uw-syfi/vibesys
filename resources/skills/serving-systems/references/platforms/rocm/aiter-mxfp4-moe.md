# Fused MXFP4 MoE kernel: decode-M issue-latency pitfall

Detail file for the "Fused MXFP4 MoE kernel sits well below HBM bandwidth but is not memory-bound" entry in [`aiter.md`](aiter.md#fused-mxfp4-moe-kernel-sits-well-below-hbm-bandwidth-but-is-not-memory-bound). Covers the from-scratch HIP fused w4a16 MoE kernel (`SGLANG_MXFP4_MOE_HIP=1`, see [`aiter.md`](aiter.md)) at decode M=16, and the seven kernel-design alternatives tried and refuted against it.

```
Symptom: the from-scratch fused w4a16 MoE kernel (SGLANG_MXFP4_MOE_HIP=1)
         profiles at 13 to 27 percent of HBM peak bandwidth at decode
         M=16, well below memory-bound territory, but MemUnitStalled is
         near zero (0.04 to 0.15 percent) and VALUBusy is under 50
         percent (33.5 to 42.1 percent) on both of its stages.
Cause:   in-block phase timing (decode M=16, per 16-row expert block):
         activation gather 2.4 us, gate K walk 64.9 us, reduce 0.3 us, up
         K walk 23.1 us, epilogue 0.2 us, 100 us total. The K loop is
         latency-bound on the per-K-step scattered 16-row activation
         gather, not on ALU work: a cold walk over a block's activation
         rows costs 2.8x a warm walk over the same data (gate vs up walk,
         identical loop structure), while the MFMA instructions issue at
         their native rate. The compiled decode (LUT lookup, exponent
         add, bf16 cast) was the cheaper of two variants measured here:
         an integer bit-pattern decode removed only about 4 percent of
         VALU work and was slower overall in context, and it is exact
         only for block exponents in [-125, 125], while the real
         checkpoint has exponent -126 in about 3 percent of scale
         blocks. (A later ISA-level audit found the compiled decode is
         not actually lean in absolute terms, a real per-element memory
         load and a higher VALU count than assumed at this altitude; see
         "MXFP4 weight decode" below. That finding does not change this
         section's own conclusion about which term dominates wall time
         at M=16.) The earlier "decode ALU chain" cause is refuted;
         MemUnitStalled near zero here is consistent with a latency-bound,
         underfed memory unit, not with the absence of a memory-side
         limiter (see profiler.md).
Fix:     no fix closes the gap yet. Seven refuted:
         - persistent grid with an atomic tile queue: bit-identical
           output, +2.8 percent at M=16, -3.5 percent at M=256. Launch
           shape and tail idle are not the term.
         - register prefetch ring of activation fragments: spills to
           scratch at prefetch depth 2 and depth 4, about 2x slower.
         - LDS-staged activation block: cuts the gate walk about 3x
           (block 106 to 34 us, gate walk 70 to 17 us, bit-identical),
           but the 36 KB LDS footprint drops resident workgroups per CU
           from 6 to 1, so per-layer time is +23 percent.
         - smaller LDS rings (4, 8, 16 KB) to keep more workgroups
           resident: occupancy is restored, but the extra refill round
           trips cost more than they save; the best ring is still 35
           percent slower than the unstaged kernel.
         - K-split accumulators: 32 to 38 percent slower, VGPR-bound (84
           VGPRs per split).
         - skinny GEMV stage 1 (no MFMA, one workgroup per block, for
           blocks with 4 or fewer real tokens): 1.64x slower at M=16; the
           activation block held in LDS caps occupancy at 1 to 2 waves
           per SIMD.
         - skinny GEMV stage 2 for the down projection: refuted end to
           end. Its 1.20x/1.29x microbenchmark win (M=16/M=64) compared
           against the scaffold stage2 kernel (0.222 ms at M=16), not
           the production templated kernel that actually dispatches
           (0.113 ms); against production the skinny kernel itself
           measures 1.5x to 1.6x slower (176 vs 113 us at M=16, 411 vs
           276 us at M=48). A paired multi-turn run (uncapped 48
           sessions, 5 reps/side, exactness gates 13/13 throughout,
           worst-case rel L2 2.73e-5 on real weights) confirmed the
           regression end to end with the switch on: median TPOT +16.8
           percent (77.49 vs 66.32 ms) and pooled p95 TTFT turn-2+ +8.7
           percent (720.9 vs 663.3 ms).
         - split-K across S workgroups for stage 1 (S in 4/8/16, T=4
           rows/workgroup, fp32 atomic partials plus a reduce+SiLU
           epilogue kernel): refuted at the first experiment, every (M,
           S) point tried. Exact (rel L2 up to 3.6e-5), but 0.49x to
           0.58x production at M=16, 0.56x to 0.63x at M=48, and 0.78x
           to 0.94x at M=192 (the speculative-decode verify shape,
           where 32.7 percent of active blocks exceed T_MAX=4 rows and
           are not owned by this kernel at all). The reduce kernel
           alone (2560 workgroups at M=16) is already about 1.7x slower
           than the entire production kernel it aims to replace (479 vs
           282 us), and every specialization spills 80 B/lane to
           scratch memory that production's kernel pays zero of.
           Splitting K multiplies workgroup-launch count by S and adds
           atomic-reduction plus scratch traffic that a single-pass,
           MFMA-based kernel never pays; that overhead exceeds the
           entire production kernel's own runtime rather than fitting
           inside memory-bandwidth headroom. The fifth memory-oriented
           rewrite in this campaign to lose to the same issue-latency
           limiter (after register prefetch, LDS staging, pre-gather,
           and skinny GEMV).
         Why the padding matters: at decode M=16 with top-10 routing,
         each 16-row block carries about 1.2 real tokens, so about 94
         percent of the MFMA rows are padding, and the kernel sits about
         3x above its bandwidth floor (0.07 ms per layer for the 233 MB
         of expert weights a rank reads per layer).
Scope:   rocm, gfx942, this kernel (mxfp4_fused_moe stage1/stage2) at
         decode M=16.
Status:  verified (phase timing), refuted fixes listed, 2026-09-11;
         skinny stage 2 refuted end to end, split-K stage 1 refuted,
         2026-09-12.
         sglang-v0.5.18-rocm700-mi30x, job 633024 (phase timing); jobs
         633006, 633013, 633019 (persistent grid, refuted); jobs 633018,
         633021 (integer decode, refuted); job 633174 (register
         prefetch and LDS-staged block, refuted); job 633180 (LDS rings,
         refuted); jobs 633173, 633179, 633182 (skinny GEMV: stage 1
         refuted, stage 2 microbenchmarked); job 633183 (skinny stage 2:
         paired benchmark and kernel trace, refuted end to end); job
         633546 (split-K stage 1, refuted).
```

## MXFP4 weight decode: a constant-memory lookup table compiles to a real per-element global load

Follow-up to the decode-M=16 phase timing above, and to the K-split stage-1 refutation in the list of seven fixes: an ISA-level audit of the compiled decode step (the function that unpacks each MXFP4 weight nibble to bf16, used by every dispatched stage1/stage2 kernel variant, not only the scaffold path) found it carries real per-element instruction and memory cost that no earlier, counter-level measurement in this campaign had isolated.

```
Symptom: A memory-oriented rewrite of this kernel's K loop (register
         prefetch, LDS staging, activation pre-gather, split-K, skinny
         GEMV) consistently loses to the production kernel even when it
         measurably improves the specific term it targets (for example,
         LDS staging cut the gate K-walk about 3x on its own). Hardware
         counters on the production kernels sit in an ambiguous middle:
         VALU busy 30 to 46 percent, achieved HBM fetch rate 12 to 40
         percent of peak, neither saturated, so neither a pure bandwidth
         fix nor a pure latency-hiding fix (more bytes in flight) closes
         the gap on its own.
Cause:   the MXFP4 dequantization lookup table (16 entries, declared
         `__constant__`) is indexed by a per-lane, per-element decoded
         nibble, so the compiler cannot broadcast it into a register: it
         compiles to one genuine `global_load_dword` per weight element
         (plus a scheduling no-op tied to it), not a cached or
         register-resident access, even though the whole table is only
         two registers' worth of data. Measured at the instruction
         level: 8.81 VALU instructions per weight element for the decode
         step alone (1.5 to 1.8x an earlier design estimate of 5 to 6),
         plus the 1.0 real memory load and 1.0 scheduling no-op per
         element that no prior measurement in this campaign had
         isolated, for roughly 11 to 12 total instructions per element
         against a roughly 4 to 6 minimal unpack-scale-multiply-add
         sequence. This is a general ROCm/HIP codegen pitfall, not
         specific to this checkpoint or kernel: a `__constant__` table
         indexed by a value that differs per SIMD lane cannot be
         broadcast-loaded by the compiler no matter how small the table
         is, and the resulting per-element global load plus its
         dependent no-op can dominate an otherwise well-tuned loop's
         instruction count without ever showing up as a bandwidth or
         occupancy problem.
Fix:     replace the memory-based lookup with a register-resident one
         built from byte-select instructions (`v_perm_b32` on this ISA)
         over compile-time immediate constants; all 16 MXFP4 levels are
         exactly representable in bf16, so the general rounding step the
         current code applies can also be skipped for this specific
         value set. A first prototype implemented the register-resident
         table as a plain runtime-indexed array instead of `v_perm_b32`
         and regressed further rather than improving on the
         constant-memory version; see the follow-up entry below. The
         `v_perm_b32`-based version this fix specifies has since been
         built, microbench-verified, and accepted end to end; see
         "Permute-based fix" below for the design and numbers.
Scope:   rocm, gfx942, this kernel's compiled decode step, and more
         generally any HIP kernel that indexes a `__constant__` table by
         a per-lane value inside a hot loop.
Status:  job-verified (root-cause mechanism verified by direct
         inspection of the compiled kernel and cross-checked against
         hardware counters on two production dispatch shapes; a first
         fix attempt regressed further instead of confirming a speedup,
         see below; the `v_perm_b32` fix that followed it is now
         implemented, microbench-verified, and accepted end to end, see
         "Permute-based fix" below).
         Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.
```

## Register-table fix attempt: a runtime-indexed array regresses further than the constant-memory load it replaced

Follow-up to the fix proposed above: it was implemented and measured. The rewrite replaces the per-lane constant-memory gather with a 16-entry bf16-bits table built once per call (from the same `ldexpf`+`to_bf16_bits` functions the old path called per element) held in a plain local array (`tbl[byte & 0xF]`, still a runtime C array index, not the `v_perm_b32` byte-select the fix above specifies) with a compile-time-constant table index so the table build itself folds to immediates.

```
Symptom: A register-resident rewrite of the decode lookup above is
         bit-exact (0 mismatches over all 2,097,152 (block_exp, byte)
         pairs in an exhaustive test; full-kernel output identical to
         the old kernel, rel L2 bit-identical, at every M tested), but
         every measured M is slower than the constant-memory kernel it
         replaced, 22 to 54 percent slower in stage1+stage2 wall time,
         worst at the two largest M where an auto-dispatched, larger
         templated kernel's own register footprint compounds with the
         rewrite's VGPR growth.
Cause:   moving the table into registers does not by itself avoid a
         per-lane, per-element runtime index: hipcc (ROCm 7.0, gfx942)
         lowers a plain array index (`tbl[byte & 0xF]`) over a
         register-resident table into a chain of compare-select
         instructions, one per table entry, rather than a single
         hardware byte-permute. Instruction-level counting confirmed
         this directly: VALU instructions per decoded element roughly
         tripled (stage1 6.3 to 17.6, stage2 8.3 to 19.6) instead of
         dropping to the predicted 3.3 to 4.3 per element, and VGPR
         usage grew enough to cost an occupancy wave at both kernels
         (stage1 82 to 113 VGPRs, 5 to 4 waves/SIMD, plus a new
         22-instruction register spill that did not exist before;
         stage2 78 to 119 VGPRs, 6 to 4 waves/SIMD). Hardware counters
         confirm the shift: VALUBusy went from 30 to 46 percent (the
         original ambiguous-middle reading) to 96 to 108 percent, an
         issue-bound kernel by construction now, not only by
         measurement. The per-element constant-memory load this
         rewrite set out to remove is genuinely gone (loads per element
         dropped 6 to 7x, confirmed at both the static-ISA and
         achieved-fetch-rate level), but removing a memory access that
         was never the true bottleneck at the cost of a much larger
         instruction count is a net loss.
Fix:     eliminating the constant-memory gather is necessary but not
         sufficient; the lookup must also be expressed so the compiler
         emits a hardware byte-permute or bit-arithmetic sequence, not
         a runtime-indexed local array. Use explicit byte-select
         intrinsics (`v_perm_b32` / `__builtin_amdgcn_perm`) over a
         packed constant, or check whether a differently-aligned
         storage layout changes the compiler's lowering choice, before
         accepting a "register-resident" rewrite as fixed. Do not judge
         a decode-lookup rewrite by "no more global load" alone; check
         the compiled instruction mix and VALUBusy before and after.
         The permute-based rewrite (see "Permute-based fix" below) is
         the one that fixed this; it is now accepted end to end.
Scope:   rocm, gfx942, this kernel's compiled decode step, and more
         generally any HIP kernel where a per-lane runtime-indexed
         table lookup is moved from constant memory to a
         register-resident array without controlling the selection
         instruction.
Status:  verified (bit-exact by exhaustive test and full-kernel diff;
         every measured M regressed 22 to 54 percent; ISA-level
         instruction counts and hardware counters both confirm the
         mechanism). Superseded by: the permute-based fix below
         (`v_perm_b32` byte-select over compile-time-immediate tables)
         replaces this runtime-indexed-array variant as the fix for the
         decode step; do not implement this variant.
         Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12,
         job-verified.
```

## Permute-based fix: register byte-permute lookup (job-verified, accepted as the default)

Follow-up to the runtime-indexed-array regression above: the `v_perm_b32` design that entry deferred to has now been built, measured at the kernel microbenchmark level, and confirmed end to end in a paired acceptance run. It supersedes the runtime-indexed-array attempt as the fix for the decode step and is accepted as the default fused MXFP4 MoE decode path; the constant-memory pitfall description near the top of this file is unchanged.

Design: an e2m1 nibble is a sign bit plus a 3-bit magnitude index into 8 possible bf16 magnitudes. `ldexpf` and round-to-nearest-even are sign-symmetric, so the 16-entry bf16-bits table collapses to two 8-byte tables (the low and high bytes of the 8 scaled magnitudes) plus a sign OR; this collapse was verified on device over all 256 block exponents x 8 magnitudes (2,048 cases), 0 mismatches. Both 8-byte tables are built once per 32-element block using the same `ldexpf` float path the original decode used, so they are exact by construction rather than by a separately-derived encoding. One `v_perm_b32` (`__builtin_amdgcn_perm`) selects four elements' low bytes from the low table, a second selects their high bytes from the high table, and two more interleave the results into bf16x2 words; the sign bit is OR'd into the unsigned high-byte plane before the interleave (3 ops per 4 elements total).

```
Symptom: (of the runtime-indexed-array fix above) resolved. Replacing
         `tbl[byte & 0xF]` with two v_perm_b32-based byte-select tables
         removes the compare-select chain the runtime index produced.
Cause:   the runtime-indexed-array fix moved the table into registers
         but still indexed it with a per-lane runtime value, which
         hipcc lowers to a compare-select chain, not a permute.
         Selecting a byte with `v_perm_b32` over compile-time-immediate
         table bytes avoids the runtime index altogether.
Fix:     verified exact: exhaustive 256 e8m0 x 256 byte values x 32
         elements per case (2,097,152 cases), 0 mismatches against the
         legacy constant-memory decode; full-kernel max abs diff 0.0 at
         M 16, 64, 337, 919. ISA, stage1 scaffold (per element): VALU
         6.31 to 2.66 (4.25 counting the permutes), loads 1.19 to 0.41,
         VGPRs 82 to 66, occupancy 5 to 7 waves/SIMD, compare/select
         chain absent. Stage2 tpl: VALU 8.27 to 4.63, VGPRs 78 to 79,
         occupancy 6 waves/SIMD. Hardware counters: VALUBusy 18 to 29
         percent (legacy 30 to 46 percent, the runtime-indexed-array
         variant 96 to 108 percent); VALU instruction count 0.53 to
         0.57x legacy. Microbenchmark, stage1+stage2 wall time vs
         legacy: 1.27x at M=16, 1.24x at M=64, 1.23x at M=337, 1.34x at
         M=919, 1.32x at M=2048. Remaining known inefficiency: the
         compiler still splits the 16-byte weight load into eight
         2-byte loads (0.41 loads/element where 0.19 is possible); not
         yet fixed.
Scope:   rocm, gfx942, this kernel's compiled decode step, and more
         generally any HIP kernel decoding a small fixed codebook (16
         entries or fewer) per lane.
Status:  job-verified (exhaustive exactness check, ISA audit, and
         hardware counters confirm the mechanism and the speedup at the
         kernel microbenchmark level; a paired end-to-end acceptance run
         then confirmed the speedup holds in the real multiturn server).
         Accepted as the default fused MXFP4 MoE decode path.
         Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.
```

Predicted (same per-layer-ratio extrapolation method used for the skinny-GEMV and split-K entries above): decode round about 14 percent faster at batch 16, prefill iteration about 9 percent faster at 337 tokens.

### Paired end-to-end acceptance, measured

Two jobs, one node each, base (accepted stack head, no permute rewrite) vs perm (this rewrite), 5 reps per side per job, identical harness-default flags on both sides otherwise (NEXTN k=3, sharded draft, overlap off, TunableOp on), gates 13/13 on every rep of every side (20/20 reps total), accept_len unchanged at both concurrencies:

| Concurrency | pooled p95 TTFT turn2+ | median TPOT |
|:--|--:|--:|
| Uncapped, 48 sessions | 466.9 -> 405.3 ms (-13.2%) | 21.35 -> 18.71 ms (-12.4%) |
| 16-session cap | 381.6 -> 341.9 ms (-10.4%) | 12.45 -> 11.89 ms (-4.5%) |

Both p95 TTFT improvements land close to the roughly 9 to 14 percent predicted range above; the TPOT improvement is close to it too at 48 sessions and a smaller but still non-noise effect at 16 sessions (per-rep ranges do not overlap at either concurrency: c48 base 20.78-22.30 vs perm 17.99-19.02 ms; c16 base 12.39-12.61 vs perm 11.69-12.21 ms), consistent with MoE being a smaller share of a shorter, lower-batch decode step at the lower concurrency.

Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Pitfall: a shared JIT build-cache directory breaks a paired kernel A/B

```
Symptom: A paired base-vs-candidate kernel comparison is meant to isolate
         one source change, but the candidate's build could silently
         land in the same directory the base side reads, or the base
         side could pick up a stale candidate build left over from a
         previous run.
Cause:   This fused MXFP4 MoE extension is JIT-built by
         `load_hip_extension()` into a directory keyed by a content hash
         of the kernel source (plus flags and torch/HIP version):
         `<SGLANG_HIP_EXT_DIR>/sglang_mxfp4_fused_moe-<hash>/`.
         `SGLANG_HIP_EXT_DIR` defaults to one shared cache directory, so
         two sides of a paired run that both leave it unset compile into
         the same place.
Fix:     Export a private `SGLANG_HIP_EXT_DIR` per side before staging
         or booting that side, so base and candidate never read or write
         the shared cache. Verify after boot that each side's server log
         names a distinct build-hash directory: this run's two sides
         built distinct hashes deterministically (identical across both
         jobs), and the shared cache directory was untouched by either.
Scope:   sglang with a JIT-built HIP/CUDA extension whose cache directory
         is controlled by an environment variable with a shared default;
         applies to any paired kernel A/B, not only this kernel.
Status:  verified. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12,
         job-verified.
```

## Next iteration: dword-wide loads and dispatch retune

The "Remaining known inefficiency" noted above (the compiler splitting the 16-byte weight load into eight 2-byte loads) was the starting point for the next optimization-loop iteration. Status: dword-wide loads (H-A) are job-verified and accepted as the default; a shared-activation-fragment variant (H-B) is refuted; the dispatch-threshold retune it motivated (H-C, `STAGE1_SCAFFOLD_BLOCK_THRESHOLD` 1024 to 160) is job-verified and accepted at 48-session concurrency, with no regression but no confirmed win yet at a 16-session cap. A final re-profile on top of this stack found MoE routed total at 1.19x its own floor at decode N=16 and 1.01x (essentially at the floor) at N=32, down from 2.19x and 2.30x for the permute-only stack; the kernel is still instruction-issue-bound by the same discriminators used throughout this campaign, but the absolute overhead over the floor has shrunk from 2.2 to 2.6x (the scaffold kernel, earlier iterations) to 0.96 to 1.36x (this kernel, this stack). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified. See [`aiter-mxfp4-moe-kernel-iterations.md`](aiter-mxfp4-moe-kernel-iterations.md#state-after-iteration-4-final-re-profile-on-the-accepted-stack-h-a--h-c-job-verified) for the designs, the final re-profile numbers, and the fp8-activation design this state closed without a build.

## Prefill-M behavior: redecode multiplicity grows the gap, a wide-tile fix is refuted twice

At prefill M (192 to 1024 tokens, not the decode M=16 case above), the fused kernel's per-layer stage1+stage2 time is 4.0x to 8.3x a refined weight-bandwidth floor (0.1614 ms/layer, 816 MiB combined weight bytes per rank per layer over 5.3 TB/s), worse than decode M's about 3x and growing with M (3.99x at M=192 up to 8.26x at M=1024). VALUBusy stays flat at 38 to 44 percent across this range, the same "issue-bound, not saturated" band the decode-M=16 case shows above, ruling out ALU saturation as the growing term. The gap tracks a redecode-multiplicity ratio instead (`blocks16/touched_experts`, the number of 16-token blocks a popular expert's weights get decoded into versus the minimum of one): 1.21x at M=192 growing to 2.19x at M=1024, i.e. a busy expert's weights are decoded up to about 2.2 times more than the minimum at the largest M tested, and this ratio's own growth shape tracks the Z/floor gap's growth almost exactly.

The natural fix, decode each weight fragment once per K-step and reuse it across two 16-row A-tiles instead of one (`SUBM=2`, doubling the per-block A-tile width), is refuted twice, on two structurally different base kernel configurations:

- On the production-dispatched "big" config (NWAVES=8/WIDE=4/UDEPTH=2): bit-exact, but 10 to 45 percent *slower* at every M tested (0.66x to 0.91x), with VGPR spill worsening from 2 to 10 spilled registers on a base that was already spilling.
- On a leaner "small" config (NWAVES=4/WIDE=2/UDEPTH=1), built specifically to test whether eliminating spill would let the lever win: spill reaches 0 (from a 0-spill baseline) and occupancy holds at 6 waves/SIMD, yet the kernel is 2.6x to 3x *slower* (0.34x to 0.39x), worse than the spilling variant above. This variant also fails exactness at every M (max_abs_diff of order 2^18 to 2^21 against a small rel_l2 of about 3e-5 to 5e-5, isolated corrupted elements, not root-caused; the code path is additive and never reached by the production dispatch).

Register spill is therefore not the cause: the mechanism generalizes across a spilling and a non-spilling base, and is instead the wide-tile epilogue's own structure, a sequential per-sub-block store, `__syncthreads()`, reduce, and write pass run twice instead of once, whose synchronization and LDS traffic cost exceeds the decode-reuse savings, and is proportionally worse on a leaner base with less compute to amortize it against. The redecode-multiplicity mechanism this lever targeted is real and does explain most of the Z/floor gap's own growth with M; the specific implementation tried to capture it is what does not work on this kernel structure.

**Follow-up: removing the doubled epilogue (single-epilogue SUBM=2) helps but still does not clear parity, and a further LDS-occupancy fix makes it worse.** A single-variable follow-up dropped the doubled epilogue read (`out_row_cache`) to fit a full `SUBM`-indexed LDS buffer at exactly gfx942's 65536-byte budget, letting the epilogue run once instead of twice. Bit-exact on the production "big" config, but still net slower everywhere (0.70x-0.95x across M=337-1024, stage1+stage2 combined), an improvement over the doubled-epilogue attempt (0.66x-0.91x at the same M) but not a win. `amdhsa_metadata` ground truth shows the flagged LDS-occupancy risk was moot: production's own LDS (32832 bytes) already blocks a second concurrent workgroup by itself, and both production and this variant sit at 128 VGPRs, so occupancy in both was already capped by VGPR/spill, not LDS; there was no occupancy headroom for the fix to protect. The one allowed revision (an atomic-add LDS reduction, cutting the epilogue's LDS footprint further to 8192 bytes, VGPR unchanged at 128) measured uniformly worse still (0.44x-0.84x), and PMC shows why: it issues 1.77x more LDS instructions than the single-epilogue variant (each `atomicAdd` a read-modify-write, with `NWAVES=8` threads contending on the same address per cell), trading a moot capacity risk for a real, measured LDS-contention cost. Both new variants sit at 128 VGPRs regardless of LDS footprint, confirming again that LDS was never the occupancy lever on this kernel. This closes the SUBM=2 lever for good: two independent implementations (doubled- and single-epilogue) and a further occupancy-targeted revision are refuted at every M tested, all landing at the same 128-VGPR occupancy ceiling, with the epilogue's own synchronization and LDS-traffic cost consistently exceeding the redecode-reuse saving it targets. See [`aiter-mxfp4-moe-kernel-iterations.md`](aiter-mxfp4-moe-kernel-iterations.md#prefill-m-single-epilogue-and-atomic-reduction-subm2-both-refuted-lever-closed) for the full numbers, PMC counters, and the correctness caveat on the atomic variant.

Scope: rocm, gfx942, this fused MXFP4 MoE kernel at prefill M (192 to 1024). Status: Z/floor gap and redecode-multiplicity mechanism verified (measured); SUBM=2 wide-tile fix refuted at every M tested across four implementations (doubled- and single-epilogue, plus an LDS-occupancy-targeted atomic revision on the latter); lever closed. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## See also

- [`aiter.md`](aiter.md): the kernel this pitfall applies to, the prefill-M microbenchmark, and the dequant-to-bf16-scratch alternative refuted at prefill M
- [`aiter-mxfp4-moe-kernel-iterations.md`](aiter-mxfp4-moe-kernel-iterations.md): the next iteration after the permute fix (dword-wide loads, a refuted shared-fragment variant, the dispatch-threshold retune, and the final re-profile that closed the fp8-activation design without a build)
- [`profiler.md`](profiler.md): the `rocprofv3` counters (`MemUnitStalled`, `VALUBusy`) used to classify this as latency-bound, not bandwidth-bound, and the in-kernel phase timing method that first separated latency-bound from ALU-bound here
