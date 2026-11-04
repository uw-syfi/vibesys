# Performance Bounds

Performance bounds bracket what a kernel could achieve if specific categories
of overhead were eliminated. Each bound is a time in nanoseconds — the
theoretical kernel duration under that bound's assumptions.

The gap between the current kernel time and a bound's time is the overhead
attributable to that category. The gap between two consecutive bounds within
a family isolates a narrower overhead. Together, the bounds form a map of
where time is being spent and where the largest optimization opportunities
lie.

These bounds currently assume single-core kernels. Hardware constants
(`dma_ddr_bandwidth`, TE peak) are per-NeuronCore values.

neuron-explorer's `recommend` tool computes these from whole-kernel Summary
fields. The same logic applies to any time interval within the kernel by
deriving the input quantities from the raw profile tables for that window.

## The bounds

### Setup

```python
import pandas as pd, numpy as np

NE = "~/.local/share/neuron-profile/profiles/global"
profile = "blocking-tiled"

# Load raw tables
metadata = pd.read_parquet(f"{NE}/{profile}@latest/Metadata.parquet").iloc[0]
dma_pkts = pd.read_parquet(f"{NE}/{profile}@latest/DmaPacket.parquet")
dma_agg  = pd.read_parquet(f"{NE}/{profile}@latest/DmaPacketAggregated.parquet")
active   = pd.read_parquet(f"{NE}/{profile}@latest/ActiveTime.parquet")
inst     = pd.read_parquet(f"{NE}/{profile}@latest/Instruction.parquet")
tensors  = pd.read_parquet(f"{NE}/{profile}@latest/TensorInfo.parquet")

# Hardware constants
# dma_ddr_bandwidth: max DMA bandwidth for a single NeuronCore (all 16 engines).
# hbm_ddr_bandwidth: max bandwidth for one HBM stack (shared across cores).
# Single-core profiles use dma_ddr_bandwidth.
dma_bw_peak = metadata['dma_ddr_bandwidth']  # bytes/s
te_peak_flops = (metadata['tensor_engine_num_rows']
                 * metadata['tensor_engine_num_cols']
                 * 2.0 * metadata['tensor_engine_clock_freq'] * 1e9)  # flops/s, per-NeuronCore

# Time window: first DMA start to last DMA end
t0 = int(dma_agg['start_ts'].min())
t1 = int(dma_agg['end_ts'].max())
total_time = (t1 - t0) / 1e9  # seconds
```

### Helpers

```python
def interval_merge_ns(starts, ends, t0, t1):
    """Wall-clock nanoseconds covered by overlapping intervals, clipped to [t0, t1)."""
    if len(starts) == 0:
        return 0
    starts = np.maximum(starts, t0)
    ends   = np.minimum(ends, t1)
    valid  = starts < ends
    starts, ends = starts[valid], ends[valid]
    order = np.argsort(starts)
    total, cur_end = 0, 0
    for s, e in zip(starts[order], ends[order]):
        if s >= cur_end:
            total += e - s
            cur_end = e
        elif e > cur_end:
            total += e - cur_end
            cur_end = e
    return total

def engine_active_ns(active_time_df, engine_name, t0, t1):
    """Wall-clock nanoseconds an engine was active within [t0, t1)."""
    eng = active_time_df[active_time_df['engine'] == engine_name]
    return interval_merge_ns(eng['start_ts'].values, eng['end_ts'].values, t0, t1)
```

### Input quantities

Each bound is a formula over a small set of quantities. For the whole
kernel, these come from Summary. For a time window `[t0, t1]`, they are
recomputed from the raw tables as shown below.

| Quantity | Whole kernel | Time window `[t0, t1]` |
|----------|-------------|------------------------|
| **total_time** | `Summary.total_time` | `t1 - t0` |
| **dma_active_time** | `Summary.dma_active_time` | Interval-merge `DmaPacket` rows overlapping `[t0, t1]`, clipped to window edges |
| **dma_transfer_bytes** | `Summary.dma_transfer_total_bytes` | `SUM(transfer_bytes)` from `DmaPacket` where `queue_type != 'instruction'` and `transfer_bytes > 4`, starting in `[t0, t1)` |
| **necessary_bytes** | `Summary.inputs_outputs_weights_size_bytes` | Sum of `TensorInfo.size` for tensors with `type` in `['IN', 'OUT']` |
| **te_active_time** | `Summary.tensor_engine_active_time` | Sum `ActiveTime.duration_ns` where `engine = 'tensor'`, clipped to `[t0, t1]` |
| **hw_flops** | `Summary.hardware_flops` | `SUM(adjusted_flops)` from MATMUL `Instruction` rows in `[t0, t1)` |
| **transpose_flops** | `Summary.transpose_flops` | Same, filtered to `tensor_instruction_type = 'TRANSPOSE'` |
| **engine_active_times** | `Summary.*_engine_active_time` | Per-engine interval merge from `ActiveTime` clipped to window |

**Interval merging.** `dma_active_time` is the wall-clock time when any of
the 16 DMA engines was active. It equals the interval-merge of all
`DmaPacket` rows (verified to match Summary within 2ns). For a time window,
clip each packet's `[start_ts, end_ts]` to the window before merging.
`ActiveTime` rows are pre-merged per engine during ingestion, so summing
`duration_ns` within the window (with clipping) gives the correct per-engine
active time.

### Memory family

Progressively remove layers of memory-side overhead, assuming all compute
is perfectly hidden behind data movement.

**memory_bound**

```
time = dma_active_time
```

Reflects the actual DMA workload: real transfer sizes, real
packet efficiency, real bandwidth utilization. Assuming all other work is 
pipelined behind data transfers.

```python
memory_bound = interval_merge_ns(
    dma_pkts['start_ts'].values, dma_pkts['end_ts'].values, t0, t1) / 1e9
```

**memory_bound_ideal**

```
time = dma_transfer_bytes / dma_bw_peak
```

Same total DMA traffic, but at peak HBM bandwidth with zero per-transfer
overhead. The gap `memory_bound → memory_bound_ideal` is the cost of DMA
inefficiency: small packets, low per-transfer throughput, inefficient dma
 transposes. Note that peak bandwidth may not be achievable for a given
transfer pattern — per-transfer overhead and packet size constraints set a
practical ceiling below the theoretical peak. 

```python
kernel_pkts = dma_pkts[(dma_pkts['queue_type'] != 'instruction')
                       & (dma_pkts['transfer_bytes'] > 4)]
window_pkts = kernel_pkts[(kernel_pkts['start_ts'] >= t0) & (kernel_pkts['start_ts'] < t1)]
dma_transfer_bytes = window_pkts['transfer_bytes'].sum()
memory_bound_ideal = dma_transfer_bytes / dma_bw_peak
```

**memory_bound_ideal_no_reloads**

```
time = necessary_bytes / dma_bw_peak
```

Only the necessary bytes — each tensor once — at peak bandwidth. The gap
`memory_bound_ideal → memory_bound_ideal_no_reloads` is the cost of
reloading data that could have stayed on-chip, measured at peak bandwidth.
At the kernel's actual (lower) bandwidth, the real cost of those reloads
is proportionally larger.

```python
necessary_bytes = tensors[tensors['type'].isin(['IN', 'OUT'])]['size'].sum()
memory_bound_no_reloads = necessary_bytes / dma_bw_peak
```

### Compute family

Progressively remove layers of compute-side overhead, assuming all data
movement is perfectly hidden behind computation.

**compute_bound**

```
time = te_active_time
```

What the kernel would take if all other engines completed instantly and
only TensorE remained. This is interval-merged wall-clock time when TE
was executing — true idle gaps (e.g., between EVENT_SEMAPHORE waits) are
excluded. (May include small gaps where instructions are not perfectly
pipelined but the engine is not idle).

```python
compute_bound = engine_active_ns(active, 'tensor', t0, t1) / 1e9
```

**compute_bound_ideal_flops**

```
time = hw_flops / te_peak_flops
```

All hardware FLOPs — including transposes — at peak throughput. The gap
`compute_bound → compute_bound_ideal_flops` is overhead within TE's active
intervals: insufficient tile size, per-instruction startup cost, small
pipelining gaps as described above, transient throttling during warmup, and
even any inflation of individual instruction durations (like seemingly
caused by data starvation).

```python
matmuls = inst[(inst['engine'] == 'Tensor') & (inst['opcode'] == 'MATMUL')
               & (inst['start_ts'] >= t0) & (inst['start_ts'] < t1)]
hw_flops = matmuls['adjusted_flops'].sum()
compute_bound_ideal = hw_flops / te_peak_flops
```

**compute_bound_ideal_useful_flops**

```
time = (hw_flops - transpose_flops) / te_peak_flops
```

Only useful FLOPs at peak throughput. The gap
`compute_bound_ideal_flops → compute_bound_ideal_useful_flops` is TensorE
time spent on transposes. Zero when the kernel has no transposes.

```python
transpose_matmuls = matmuls[matmuls['tensor_instruction_type'] == 'TRANSPOSE']
transpose_flops = transpose_matmuls['adjusted_flops'].sum()
compute_bound_useful = (hw_flops - transpose_flops) / te_peak_flops
```

### Pipeline family

**perfect_pipeline**

```
time = max(engine_active_times)
```

With perfect overlap, the kernel duration equals the slowest engine. The
gap `total_time → perfect_pipeline` is serialization overhead — engines
idle waiting for each other. The bottleneck engine (the arg-max) identifies
which optimization family has the most leverage.

```python
engine_times = {
    'Tensor': engine_active_ns(active, 'tensor', t0, t1) / 1e9,
    'Vector': engine_active_ns(active, 'vector', t0, t1) / 1e9,
    'Scalar': engine_active_ns(active, 'scalar', t0, t1) / 1e9,
    'GpSimd': engine_active_ns(active, 'gpsimd', t0, t1) / 1e9,
    'DMA':    memory_bound,
}
bottleneck_engine = max(engine_times, key=engine_times.get)
perfect_pipeline  = engine_times[bottleneck_engine]
```

### Bottleneck-aware speedup

Each memory or compute bound improves one engine family. The remaining
engines are unchanged. The achievable speedup is capped by whichever
non-improved engine is slowest:

```
bn_speedup = total_time / max(bound_time, max_non_impacted_engine_time)
```
In practice, engine execution times can be intertwined. For example, improving
memory efficiency might reduce Tensor Engine time as it is less often throttled
after idle gaps.

## Reading the gaps

Each pair of consecutive bounds within a family isolates a specific
overhead. The gap — the difference in time between two bounds — tells you
how much of the engine's time is attributable to that category of overhead.

### Memory family gaps

```
1. memory_bound ─→ total_time           : DMA idle gaps 
2. memory_bound ─→ memory_bound_ideal   : DMA inefficiency (BW utilization)
3. memory_bound_ideal ─→ no_reloads     : excess HBM traffic (reloads + spills)
```

All three of these gaps can be interpreted as lost time under the assumption that DMA
is the bottleneck engine. Gap 1 tells us time lost to DMA idle gaps. Gap 2 tells us time
lost to low bandwidth utilization but is theoretical and may not be achievable. Gap 3 
should now be intepreted / approximated as a proportional loss (since it the gap is counted at 
ideal bw utilization). Excess inefficient loads potentially contribute more to gap 2 then 3. 

Technically, gap 1 would need to go to 0 for gap 2 to be interpreted exactly as lost time and so on. 
This is because reducing gap 2, even in a memory bound kernel, may not lead to an improvement in a poorly pipelined case
as another engine might be active anyways at that time. 

### Compute family gaps

```
1. total_time ─→ compute_bound                    : TE idle gaps
2. compute_bound ─→ compute_bound_ideal_flops     : TE underutilization (within active intervals)
3. compute_bound_ideal_flops ─→ ideal_useful      : transpose overhead
```

As in the memory case, these gaps can be intepreted as lost time under the assumption that Tensor Engine
is the bottleneck. Gap 1 and 2 can be interpreted as raw values with the same caveats as before. Gap 3
should be intepreted as a proportion of TE engine time. 

### Pipeline gap

In the case where neither DMA nor Tensor Engine is the bottleneck, we are interested
in the gap to the bottleneck engine.  
```
total_time ─→ perfect_pipeline  : total serialization across all engines
```

If this gap is small, we will need to look at ineffiency and redundant computation on that engine 
but the calculation is non-trivial. If the gap is large, pipelining across engines should be the
priority. This skill does not yet support it but you may extrapolate from the existing investigations
if EXPLICITELY permitted.  

### Gaps are not improvement deltas

The gaps measure overhead *within a category*. They do not predict how much
faster the kernel will be if that overhead is eliminated. Three reasons:

**1. Gaps are computed under idealized assumptions.** The memory family gaps
assume peak HBM bandwidth. The compute family gaps assume peak TE
throughput. Real optimizations don't achieve these ideals — there is always
residual overhead from transfer setup, instruction startup, and hardware
constraints. The gaps are lower bounds on per-category overhead, not
achievable savings.

**2. The engine may not be on the critical path.** A large gap in the
memory family doesn't matter if DMA is already faster than TensorE. The
`bn_speedup` metric accounts for this by capping the speedup at the
non-impacted engine's time, but the gap itself does not. Always check
whether the engine you're optimizing is actually the bottleneck before
investing in reducing its overhead.

**3. Engine times are not independent.** Fixing one engine can change
another's reported time. The clearest example: in a data-starved kernel,
TensorE's active time is inflated because individual MATMUL instructions
take longer when weight data arrives late. Reducing DMA overhead (a memory
optimization) feeds TensorE faster, which shrinks TE active time — even
though no compute optimization was applied. This means the compute family
bounds computed from the *current* TE active time overstate the compute
overhead that would remain after fixing the memory side. The reverse can
also occur: improving TE pipelining can reduce the DMA working set if it
changes how tiles are scheduled.

### What gaps are useful for

Gaps tell you **where overhead lives** and **how it's distributed across
categories**. Use them to:

- **Identify the dominant overhead**: a large gap within the
  bottleneck engine's family may be the highest-leverage investigation target.
- **Compare before/after**: computing a specific gap for a kernel before
  and after an optimization shows whether that optimization reduced the
  targeted overhead category.
- **Detect diminishing returns**: when a gap shrinks close to zero,
  further optimization in that category won't help — the remaining time
  is in other gaps or other engines.

## From bounds to investigations

The bounds produce a gap structure for a kernel. This section maps each
gap to an optimization group and lists the available investigations.

### Inefficiency groups

For all engines there are the following *groups* of inefficiencies. For 
(1) Tensor Engine and (2) DMA, and (3) other bottleneck engine, we defin the following 
groups: (a) Idle gaps -> (b) engine underutilization -> (c) redundant instructions. 

**Group 1a: DMA engine idle gaps**
Gap: `total_time → memory_bound`

DMA idle gaps are tough to interpret, do not make a statement about the cause of this gap. 

**Group 1b: DMA inefficiency**
Gap: `memory_bound → memory_bound_ideal`

[DMA Efficiency investigation](investigations/dma_efficiency.md)

Covers: 
- Dma transfer sizes

**Group 1c: Redundant DMA transfers from HBM**
Gap: `memory_bound_ideal → memory_bound_ideal_no_reloads`

[Redundant dma transfers investigation](investigations/redundant_dma_transfers.md)

Covers:  
- Excess Input Reloads
- Intermediate Data Spilling

**Group 2a: TE idle gaps**
Gap: `total_time → compute_bound`

This gap can be difficult to interpret due to the currently available information
regarding dependencies and anti-dependencies. Coming soon!  

**Group 2b: Compute engine underutilization**
Gap: `compute_bound → compute_bound_ideal_flops`

The `→ ideal_flops` gap covers multiple optimizations that the bounds
cannot separate — insufficient tile sizes, instruction placement, fast weight load, and
throttling all live in the same gap.

Investigations:
- [TE Inefficiency](investigations/te_inefficiency.md)

**Group 2c: Redundant TE engine instructions**
Gap: `compute_bound_ideal_flops → compute_bound_ideal_useful_flops`

Investigations:
- [Redundant TE Transposes](investigations/redundant_te_transposes.md)

**Group 3: Efficiency gaps when Vector/Scalar/Gpsimd is the bottleneck**
`perfect_pipeline > max(memory_bound, compute_bound)`

When the bottleneck is neither DMA nor Tensor Engine, analysis is less
straightforward since Vector, Scalar and Gpsimd play diverse roles within
the kernel. Gap 3a (idle gaps) can be found as `total_time → perfect_pipeline`
but defining efficiency and "minimal" workload is instruction implementation
 specific. 

### Reading the gap structure

If an engine is by far the bottleneck, focus on that family of gaps. In practice
multiple engines may be quite close, and in this case, focus on all bottlenecks. In
such a case, logically section the kernel and run windowed analysis. 

Within a family, the relative sizes of the gaps indicate where overhead
concentrates. However, gap c should not be read as an absolute value, it is inherently
proportional to the engine workload. 

Gaps are not mutually exclusive (even across families) — a kernel can have
significant overhead in multiple gaps simultaneously, and addressing one
gap can shift others (see "Gaps are not improvement deltas" above).

After applying an optimization, re-profile and recompute bounds. The gap
structure will shift. The bottleneck engine may change. The new gap
structure guides the next investigation.
