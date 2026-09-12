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
         built and is microbench-verified; see "Permute-based fix"
         below for the design and numbers. It has not yet passed the
         paired end-to-end acceptance test, so no serving-level speedup
         is claimed here.
Scope:   rocm, gfx942, this kernel's compiled decode step, and more
         generally any HIP kernel that indexes a `__constant__` table by
         a per-lane value inside a hot loop.
Status:  microbench-verified (root-cause mechanism verified by direct
         inspection of the compiled kernel and cross-checked against
         hardware counters on two production dispatch shapes; a first
         fix attempt regressed further instead of confirming a speedup,
         see below; the `v_perm_b32` fix that followed it is now
         implemented and microbench-verified, see "Permute-based fix"
         below, but has not yet passed end-to-end acceptance).
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
         The permute-based rewrite is still in progress and not yet
         measured; no speedup is claimed for it here.
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

## Permute-based fix: register byte-permute lookup (microbench-verified, not yet accepted end to end)

Follow-up to the runtime-indexed-array regression above: the `v_perm_b32` design that entry deferred to has now been built and measured. It supersedes the runtime-indexed-array attempt as the fix for the decode step; the constant-memory pitfall description near the top of this file is unchanged.

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
Status:  microbench-verified (exhaustive exactness check, ISA audit,
         and hardware counters all confirm the mechanism and the
         speedup at the kernel microbenchmark level). This has NOT yet
         passed the paired end-to-end acceptance test; the round-level
         numbers below are predictions from the microbenchmark ratio,
         not serving measurements, until that gate runs.
         Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.
```

Predicted, not measured (same per-layer-ratio extrapolation method used for the skinny-GEMV and split-K entries above, not a serving measurement): decode round about 14 percent faster at batch 16, prefill iteration about 9 percent faster at 337 tokens. Do not cite these as serving numbers until the paired end-to-end acceptance test runs.

## See also

- [`aiter.md`](aiter.md): the kernel this pitfall applies to, the prefill-M microbenchmark, and the dequant-to-bf16-scratch alternative refuted at prefill M
- [`profiler.md`](profiler.md): the `rocprofv3` counters (`MemUnitStalled`, `VALUBusy`) used to classify this as latency-bound, not bandwidth-bound, and the in-kernel phase timing method that first separated latency-bound from ALU-bound here
