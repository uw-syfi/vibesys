# Getting Started with Profile Analysis

This skill lets you query and analyze NKI kernel execution profiles on
Neuron hardware (Trainium/Inferentia). It works with NEFF (compiled kernel), NTFF (execution trace) and corresponding 
parquet files produced by `neuron-explorer`.

## What it can do

- **Query profile data**: Run SQL or Python against parquet tables
  (Summary, Instruction, DmaPacket, DmaPacketAggregated, ActiveTime, etc.)
  to answer specific questions about kernel execution.

- **Compute performance bounds**: Calculate memory and compute family
  bounds to identify the bottleneck engine and quantify gaps (idle time,
  engine underutilization, redundant work).

- **Run investigations**: Trace specific inefficiencies (excessive reloads,
  intermediate spilling, DMA transfer sizes, compute tile sizes, transposes,
  DMA-compute pipelining) to NKI source lines.

- **Compare versions**: Profile before and after an optimization, present
  side-by-side bounds and gaps to show what changed.

## What it needs

- A compiled NEFF file and a captured NTFF trace file on disk.
- `neuron-explorer` installed (comes with AL2023 DLAMI).
- For full analysis: profile captured with `NEURON_RT_ENABLE_DGE_NOTIFICATIONS=1`
  and kernel compiled with `NEURON_FRAMEWORK_DEBUG=1`.

## Example prompts

### 1. Simple query

```
I have a profiled kernel at `./output/kernel.neff` and `./output/profile.ntff`.
What is the total execution time, and which engine is most active? 
```

This will ingest the profile, query the Summary table, and report
`total_time` and per-engine active time percentages.

### 2. Full bounds analysis
```
Profile and analyze the NKI kernel at `kernels/matmul_blocked.py`.
The kernel takes lhsT[4096,4096] bf16 and rhs[4096,4096] bf16.
Compute performance bounds, identify the bottleneck, and run the
relevant investigations. Save the report to `analysis/matmul_report.md`.
```

This will:
1. Compile and profile the kernel with the right env vars
2. Ingest into neuron-explorer
3. Compute all bounds (memory, compute, pipeline families)
4. Identify dominant gaps
5. Run investigations matching the bottleneck engine's gaps
6. Present a report with bounds table, engine times, gaps, and
   per-investigation findings

### 3. Windowed investigation

> Using the profile `swiglu-mlp-v6` already ingested at
> `~/.local/share/neuron-profile`, run the DMA-Compute Pipelining
> investigation on the first 5,000 us of execution. How many MATMULs
> are DMA-starved in that window vs the rest of the kernel?

This will:
1. Load the parquet tables
2. Set the time window to [t0, t0 + 5000 us]
3. Run the excess initiation interval analysis within that window
4. Compare DMA-gated vs TE-gated MATMULs in the window vs full kernel

## Reference

- [SKILL.md](../SKILL.md) — Full agent workflow (Steps 0–4, Profile Analysis)
- [performance-bounds.md](performance-bounds.md) — Bound definitions, gap interpretation, investigation groups
- [example-bounds-analysis.md](example-bounds-analysis.md) — Worked example: matmul V0→V1→V2
- [investigations/](investigations/) — Per-investigation detection and localization steps
- [schema/](schema/) — Table and field documentation
