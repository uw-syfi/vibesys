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
         value set. A register-resident prototype of this fix is in
         progress; it is not yet measured end to end, so no speedup is
         claimed here.
Scope:   rocm, gfx942, this kernel's compiled decode step, and more
         generally any HIP kernel that indexes a `__constant__` table by
         a per-lane value inside a hot loop.
Status:  candidate (ISA-level mechanism confirmed by direct inspection
         of the compiled kernel and cross-checked against hardware
         counters on two production dispatch shapes; the register-
         resident fix itself is not yet measured). Stamp:
         sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.
```

## See also

- [`aiter.md`](aiter.md): the kernel this pitfall applies to, the prefill-M microbenchmark, and the dequant-to-bf16-scratch alternative refuted at prefill M
- [`profiler.md`](profiler.md): the `rocprofv3` counters (`MemUnitStalled`, `VALUBusy`) used to classify this as latency-bound, not bandwidth-bound, and the in-kernel phase timing method that first separated latency-bound from ALU-bound here
