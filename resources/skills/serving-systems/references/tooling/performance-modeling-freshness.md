# Performance modeling: freshness gate and worked pitfall examples

Follow-up to [`performance-modeling.md`](performance-modeling.md): when to refresh a calibrated model, and worked examples of specific ways a model or a lever choice goes wrong even when the reasoning looked sound at the time.

## Freshness and calibration gate

A copied model is not a refreshed model. After a scheduler, cache layout,
kernel backend, precision, graphing strategy, or transport path changes, verify
that every architecture-dependent statement still describes the production
path. Record a candidate commit or equivalent architecture fingerprint and the
exact artifacts used for calibration.

Before using the model to rank another optimization, require all of:

1. Numerical FLOPs and byte assumptions for the relevant prefill/decode regime.
2. Usable device compute and bandwidth ranges with their source or discount.
3. A hardware/workload ceiling distinct from the current implementation knee.
4. A prediction for at least one retained operating point, its observed value,
   calibration error, and the residual the model does not explain.
5. TTFT, TPOT, end-to-end latency, failures, and accuracy treated as constraints
   rather than inferred from throughput.

Reject the model as stale when it names a removed bottleneck, contradicts
activation telemetry, cannot reproduce any retained measurement, or reports a
measured saturation point as though it were the hardware roofline.

## Worked pitfall examples

### A kernel variant wins its microbenchmark and regresses the serving metric

```
Symptom: a kernel variant wins its microbenchmark by 1.2 to 1.3x and
         regresses the serving metric by 17 percent end to end.
Cause:   the microbenchmark baseline was a different kernel from the one
         production dispatches (a scaffold path vs the templated path
         chosen at the production dispatch threshold).
Fix:     the microbenchmark baseline must be the production dispatch
         path at the production shapes; confirm by name against a
         kernel trace of the server before integrating.
Scope:   any kernel-variant comparison feeding an integration decision,
         backend-independent.
Status:  verified. 2026-09-12, job 633183.
```

### A lossy-format change is only worth its accuracy cost if it attacks the live bound

```
Symptom: a lower-precision kernel design promises fewer arithmetic
         instructions per element, but the exact kernel it would
         replace is already close to a different bound (a byte-
         movement floor), not an instruction-issue ceiling.
Cause:   a lossy format change only pays off against the bound it
         actually moves. A narrower activation format can cut matrix-
         instruction issue count without touching the weight bytes a
         byte-bandwidth floor is built from; if the exact kernel is
         already close to that floor, cutting issue count further
         cannot close the remaining gap.
Fix:     name which bound (compute, byte movement, launch/latency) the
         format change attacks, then check the exact kernel's distance
         from that specific bound, not an aggregate wall-clock or ALU-
         utilization number, before spending accuracy budget on it.
Scope:   any lossy numerics change proposed against a kernel with an
         existing exact implementation, backend- and engine-agnostic.
Status:  verified (an fp8-activation MoE kernel design predicted an 8x
         cut in matrix-instruction issue count, but the exact kernel it
         would replace was already within 1.0 to 1.2x of the weight-
         byte floor the format change does not touch, so the design
         was closed before implementation). Stamp: sglang-v0.5.18-
         rocm700-mi30x, 2026-09-13, job-verified.
```

### A kernel can match its own byte count exactly and still be far from its bandwidth floor

```
Symptom: a kernel's fetched-byte counter matches its hand-computed
         required-read-bytes figure almost exactly (no over-fetch), yet
         measured time is 5 to 9x its byte-bandwidth floor and achieved
         fetch rate is a small fraction (roughly a tenth) of peak
         bandwidth. VALU busy is low and VALU-per-element is far under
         any instruction-issue-bound threshold, so the kernel is not
         compute-bound either.
Cause:   the kernel is launched as many small, independent grids (for
         example one launch per layer, each a handful of single-warp
         programs) instead of one grid spanning all the independent
         work. Each small launch is too few total waves across the
         device to hide its own HBM round-trip latency, regardless of
         how exactly its byte count matches the data it touches. This is
         a launch-granularity / occupancy problem, not a memory-access-
         pattern or arithmetic-density problem, and neither the
         fetched-bytes counter nor the VALU-per-element count can see it
         on their own.
Fix:     pack the independent axis (layers, heads, or any other
         axis the small launches iterate over in a host-side loop) into
         additional grid dimensions of one launch, the way a sibling
         kernel that already does this over the same axis demonstrates
         is possible. Check for such a sibling first: a kernel touching
         the same data structure that already packs the axis into its
         grid is the reference point for how much the fused launch
         should recover, and existence proof that the fusion is safe.
Scope:   any kernel-internal profile where FETCH_SIZE is within a few
         percent of the computed byte requirement but VALUBusy is low
         and time is several times the byte floor, backend- and
         engine-agnostic.
Status:  verified. Mechanism confirmed by ISA and counter audit and
         cross-validated against a production trace to within 3 percent;
         the fix itself (layer-axis grid packing) was then built and
         measured in isolation: exact against the unpacked baseline and
         6.17x/2.50x/1.82x faster at three batch sizes, the shape
         predicted. On the one kernel pair this was tried on, the fix
         could not be wired into production; see the next example for
         the dependency check that would flag this before implementing.
         Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
         microbench-verified.
```

### A launch-packing fix can be exact and fast in isolation yet unreachable in production

```
Symptom: a candidate fix packs many small per-item launches into one
         larger launch, is bit-exact against the unpacked baseline, and
         measures several times faster in an isolated microbenchmark,
         matching or exceeding the predicted magnitude, and still
         cannot be shipped.
Cause:   the "items" being packed were never independent data at any
         single point on the production call path. Each one was
         produced by, and its output consumed by, a per-item step in a
         sequential loop (for example one step per decoder layer, each
         step's output feeding the next step's input), so all items'
         inputs are never simultaneously available to hand to one grid.
         A sibling kernel elsewhere may already pack the same axis
         successfully; that only proves the data layout supports
         packing, not that this call site's data dependency does.
Fix:     before implementing a launch-packing round, trace whether the
         launches being packed are independent at the point they would
         be packed: does launch i's output feed launch i+1's input
         before launch i+1 runs? If yes, packing across that axis needs
         restructuring the surrounding control flow, a materially
         bigger change than a kernel round, not just a grid change;
         scope the round to an isolated diagnostic build instead of a
         production integration, and say so up front.
Scope:   any launch-count-reduction hypothesis where the launches being
         merged are dispatched from a host-side loop with an
         intervening per-iteration consumer, backend- and
         engine-agnostic.
Status:  verified. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
         microbench-verified.
```

### Widening a launch's own grid is not the same lever as cutting launch count

```
Symptom: a launch-granularity-bound kernel is made to run more grid
         programs per launch (a finer tile split) while its launch
         count stays fixed, hoping the extra programs hide latency the
         way more total waves did for a sibling kernel that packs its
         axis into fewer, larger launches instead. Every measured cell
         comes out slower than the unmodified kernel, and the slowdown
         grows both with the split factor and with batch size, the
         opposite of the predicted direction and trend.
Cause:   launch count, not per-launch grid size, was the fixed cost the
         hypothesis meant to amortize. Splitting a tile across more
         grid programs does not touch launch count, and each added
         program reloads and re-derives shared per-program setup work
         (gating, normalization, or other per-tile inputs) that used to
         run once; that duplicated work is real, growing cost with no
         offsetting latency-hiding gain when occupancy under the
         unmodified grid was already adequate.
Fix:     name which fixed cost a lever actually removes before running
         it. A kernel that is slow because of too few launches needs
         fewer, larger launches (pack the independent axis into the
         grid); a kernel that is slow because of too few waves within
         an already-large launch needs a finer tile. Confirm which
         regime applies from the audit (occupancy and wave count at the
         current grid) before choosing between them; they are not
         interchangeable, and applying the wrong one adds cost instead
         of removing it.
Scope:   any launch-granularity-bound kernel where a grid-widening fix
         is proposed as an alternative to reducing launch count,
         backend- and engine-agnostic.
Status:  verified. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
         microbench-verified.
```

## See also

- [`performance-modeling.md`](performance-modeling.md): the main analytical-modeling workflow this file follows up on
- [`profiler.md`](profiler.md): the issue-bound-vs-latency-bound discriminator and the bucket-fold comparison pitfall
- `platforms/rocm/gated-delta-net.md`: the worked kernel pair (update kernel launch-granularity-bound, fold kernel already packs the same axis) the launch-granularity examples above generalize from, and the full account of the packing round this file's dependency-check and grid-widening examples are drawn from
