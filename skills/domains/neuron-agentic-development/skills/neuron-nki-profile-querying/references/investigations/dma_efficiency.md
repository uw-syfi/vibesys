# Investigation: DMA Transfer Efficiency

## Context

This investigation follows from a large `memory_bound → memory_bound_ideal` gap in the
[performance bounds](../performance-bounds.md). That gap measures the cost
of DMA inefficiency: small packets and low per-transfer throughput. `memory_bound` is the actual wall-clock time when any DMA engine was active.
`memory_bound_ideal` is the same total bytes at peak HBM bandwidth.

The bounds identify that DMA is spending more wall-clock time than it would at peak
bandwidth, but not why. The investigation covers one potential source for this gap:

- **Small transfer sizes** — each DMA transfer has a per-transfer and per-packet overhead.
  Small transfers spend a larger fraction of their time on overhead vs actual data
  movement, reducing effective bandwidth.

This investigation quantifies the efficiency gap and traces it to specific source lines. 
DMA transposes from HBM to SBUF contribute to dma inefficiency but aren't covered here 
yet. 

Note: the efficiency gap often coexists with excess HBM traffic. Excess inefficient traffic
can pump up both the inefficiency gap and the excess gap. If the excess gap is proportionally
large, it potentially represents that proportion of the inefficiency gap as well. Consider 
investigating that one first in such a case since reducing it will potentially reduce the 
number of inefficient transfers.

## Prerequisites

- Profile captured with `NEURON_RT_ENABLE_DGE_NOTIFICATIONS=1` (produces the
  `DmaPacketAggregated` table)
- NEFF compiled with `NEURON_FRAMEWORK_DEBUG=1` (produces `bir_debug_info_source_location`
  on instructions)

## Step 1: Detect — quantify the efficiency gap

### Setup

```python
import pandas as pd, numpy as np, os

NE = os.path.expanduser("~/.local/share/neuron-profile/profiles/global")
profile = "blocking-tiled"  # replace with your profile name
d  = f"{NE}/{profile}@latest"

dma_pkts = pd.read_parquet(f"{d}/DmaPacket.parquet")
metadata = pd.read_parquet(f"{d}/Metadata.parquet").iloc[0]

dma_bw_peak = metadata['dma_ddr_bandwidth']  # bytes/s

# Kernel DMA packets (excludes infrastructure)
kernel_pkts = dma_pkts[(dma_pkts['queue_type'] != 'instruction')
                       & (dma_pkts['transfer_bytes'] > 4)]
```

### Time window

All queries scope to `[t0, t1]`. Derived from the filtered kernel DMA
packets to cover all transfer modes. Override `t0`/`t1` to zoom into a
specific phase.

```python
# Find the minium packet start and max packet end of the filtered packets
t0 = int(kernel_pkts['start_ts'].min())
t1 = int(kernel_pkts['end_ts'].max())
```

### Interval merge helper

16 DMA engines run in parallel. Raw `DmaPacket.duration_ns` sums are much
larger than wall-clock time. Always interval-merge before comparing with
other metrics.

```python
def interval_merge_ns(starts, ends, t0, t1):
    """Wall-clock nanoseconds covered by overlapping intervals, clipped to [t0, t1)."""
    starts = np.maximum(starts, t0)
    ends   = np.minimum(ends, t1)
    valid  = starts < ends
    starts, ends = starts[valid], ends[valid]
    if len(starts) == 0:
        return 0
    order = np.argsort(starts)
    s, e = starts[order], ends[order]
    total, cur_end = 0, 0
    for si, ei in zip(s, e):
        if si >= cur_end:
            total += ei - si
            cur_end = ei
        elif ei > cur_end:
            total += ei - cur_end
            cur_end = ei
    return total
```

### Compute the gap

```python
# All packets within the kernel time window
window_pkts = dma_pkts[(dma_pkts['start_ts'] >= t0) & (dma_pkts['start_ts'] < t1)]

dma_active_ns      = interval_merge_ns(dma_pkts['start_ts'].values,
                                        dma_pkts['end_ts'].values, t0, t1)
dma_transfer_bytes = window_pkts['transfer_bytes'].sum()

memory_bound       = dma_active_ns / 1e9
memory_bound_ideal = dma_transfer_bytes / dma_bw_peak
efficiency_gap_us  = (memory_bound - memory_bound_ideal) * 1e6
achieved_bw        = dma_transfer_bytes / memory_bound if memory_bound > 0 else 0

print(f"memory_bound:       {memory_bound * 1e6:>8.0f} us")
print(f"memory_bound_ideal: {memory_bound_ideal * 1e6:>8.0f} us")
print(f"efficiency_gap:     {efficiency_gap_us:>8.0f} us")
print(f"achieved_bw:        {achieved_bw / 1e9:>8.0f} GB/s  (peak: {dma_bw_peak / 1e9:.0f} GB/s)")
```

If `efficiency_gap_us` is small relative to `memory_bound` (e.g. < 5%), DMA
transfers are already reasonably efficient. If the kernel is still memory-bound,
the issue might be excess traffic (reloads/spills), not per-transfer efficiency.


## Step 2: Localize — per-source-line transfer geometry

### Check DPA coverage

Step 2 joins Instruction → DmaPacketAggregated via the Flow table.
DPA may only cover a subset of kernel transfers depending on the DGE
mode used (see Known Issues). Check coverage first — if DPA is missing, 
skip step 2 entirely and if it undercounts significantly, note that 
Step 2 results will be incomplete. 

```python
dma_agg_path = f"{d}/DmaPacketAggregated.parquet"
if not os.path.exists(dma_agg_path):
    print("DmaPacketAggregated: MISSING — Step 2 cannot run")
    dma_agg = None
else:
    dma_agg = pd.read_parquet(dma_agg_path)
    dpa_bytes = dma_agg['transfer_bytes'].sum()
    coverage = dpa_bytes / dma_transfer_bytes * 100 if dma_transfer_bytes > 0 else 0
    print(f"DPA coverage: {dpa_bytes}/{dma_transfer_bytes} bytes ({coverage:.0f}%)")
    if coverage < 95:
        print(f"  {100 - coverage:.0f}% of kernel DMA lacks variable attribution")
```

### Source-line attribution

Join HBM transfer instructions to DmaPacketAggregated via the Flow
table. For each source line, report the transfer geometry.

```python
inst = pd.read_parquet(f"{d}/Instruction.parquet")
flow = pd.read_parquet(f"{d}/Flow.parquet")

window_agg = dma_agg[(dma_agg['start_ts'] >= t0) & (dma_agg['start_ts'] < t1)
                     & (dma_agg['is_transpose_mode'] != True)]

# Flow rows linking instructions to their DMA transfers
inst_to_agg = flow[(flow['in_table'] == 'Instruction')
                   & (flow['out_table'] == 'DmaPacketAggregated')]

# HBM transfer instructions carry source attribution regardless of DGE mode
hbm_xfers = inst[((inst['hbm_read_bytes'] > 0) | (inst['hbm_write_bytes'] > 0))
                  & (inst['bir_debug_info_source_location'].notna())].copy()
hbm_xfers['src'] = hbm_xfers['bir_debug_info_source_location'].apply(
    lambda x: x.split('/')[-1] if pd.notna(x) else '?')

src_to_agg = inst_to_agg.merge(
    hbm_xfers[['id', 'src']].rename(columns={'id': 'in_id'}),
    on='in_id', how='inner')
src_to_agg = src_to_agg.merge(
    window_agg[['id', 'transfer_bytes', 'source', 'variable',
                 'write_num_sbuf_partitions', 'read_num_sbuf_partitions']].rename(
        columns={'id': 'out_id'}),
    on='out_id', how='inner')

for src_name, grp in sorted(src_to_agg.groupby('src'),
                              key=lambda x: -x[1]['transfer_bytes'].sum()):
    n = len(grp)
    total_bytes = grp['transfer_bytes'].sum()
    tensor = grp['variable'].mode().iloc[0]
    is_load = grp['source'].iloc[0] == '[[INPUT]]'
    direction = 'load' if is_load else 'store'

    xfer_bytes = int(grp['transfer_bytes'].median())
    partitions = int(grp['write_num_sbuf_partitions'].median()) if is_load \
        else int(grp['read_num_sbuf_partitions'].median())
    desc_bytes = xfer_bytes // partitions if partitions > 0 else xfer_bytes

    pct = total_bytes / dma_transfer_bytes * 100 if dma_transfer_bytes > 0 else 0
    print(f"  {src_name:<45} {tensor:<10} {direction:>5}  "
          f"xfers={n:>5}  {total_bytes/1e6:>6.1f} MB ({pct:>4.1f}%)  "
          f"shape={partitions}x{desc_bytes}B")
```

Each row is a `nisa.dma_copy` call. `shape=PxDB` shows the partition
count (P) and the descriptor size in bytes (D = `transfer_bytes / P`).
For a contiguous load, the descriptor size is `F x element_size` where F
is the free dimension.

DMA throughput correlates with descriptor size. Transfers with descriptor
sizes below 4 KB are significantly less efficient. Aim for `desc_bytes`
of at least 4 KB — widen the free dimension of the `dma_copy` to get
there. Beyond 4 KB, improvements are marginal and may trade off against
pipelining.

Transposed loads are also less efficient than normal loads.

## Worked examples

### Mixed DGE kernel (128x512 tiles)

`dge_mixed.py`: 16 load+store tile transfers across 4 DGE modes (none,
swdge, hwdge, unknown), plus 4 HBM→SBUF transposed loads and 4 SBUF→SBUF
transposes. Input: 128x8192 bf16.

### Step 1: Detect

| Metric | Value |
|--------|-------|
| memory\_bound | 16 us |
| memory\_bound\_ideal | 10 us |
| efficiency\_gap | 7 us |
| achieved\_bw | 259 GB/s (peak: 435 GB/s) |

The dma engine is clearly operating way below expected efficiency. 

### Step 2: Localize

DPA coverage: 50% — swdge and unknown transfers missed variable attribution.

| Source line | Tensor | Dir | Xfers | MB | % of total | Shape |
|-------------|--------|-----|------:|---:|-----------|-------|
| dge\_mixed.py:21 | input0 | load | 4 | 0.524 | 12.3% | 128x1024B |
| dge\_mixed.py:22 | output0 | store | 4 | 0.524 | 12.3% | 128x1024B |
| dge\_mixed.py:35 | input0 | load | 4 | 0.524 | 12.3% | 128x1024B |
| dge\_mixed.py:36 | output0 | store | 4 | 0.524 | 12.3% | 128x1024B |
| dge\_mixed.py:51 | output0 | store | 4 | 0.016 | 0.4% | 16x256B |

From the covered lines analyzed, we can see that almost 50% of the kernel's transfer
bytes are from transfers with small descriptor sizes (1kib instead of 4Kib). Increasing the free dimension
of these transfers may improve the DMA efficiency of the kernel. 

### Comparison: 128x2048 tiles

Same kernel structure with 4x larger tiles (`dge_mixed_large.py`).

| Metric | 128x512 | 128x2048 |
|--------|---------|----------|
| memory\_bound | 16 us | 47 us |
| memory\_bound\_ideal | 10 us | 39 us |
| efficiency\_gap | 7 us | 9 us |
| achieved\_bw | 259 GB/s | 354 GB/s |

Descriptor sizes scale from 1024B to 4096B. Achieved bandwidth improves
from 259 to 354 GB/s with larger transfers. 

## Known issues

- **Peak bandwidth is not achievable.** `memory_bound_ideal` divides total bytes
  by peak HBM bandwidth. Real DMA always has per-transfer overhead, so achieved
  bandwidth is strictly below peak. The gap is a lower bound on DMA inefficiency,
  not a prediction of achievable improvement.

- **DGE mode may affect available tables**: Depending on the NKI and
  neuronx-cc version, the `DmaPacketAggregated` table and the
  Instruction→DmaPacketAggregated flow link may only be produced for
  `dge_mode=hwdge` and `dge_mode=none`. Profiles using `dge_mode=swdge`
  (or `unknown` when the compiler selects swdge) may lack both — if so,
  Step 2 cannot run.

- **Instruction→DPA flow edges may be incomplete**: A small number of
  Instruction or DmaPacketAggregated rows may lack a corresponding flow
  edge. Step 2 results may be slightly incomplete as a result. Step 1
  totals are unaffected since they use DmaPacket directly.

- **Identifying transfer instructions**: Use `hbm_read_bytes > 0` for
  HBM→SBUF loads and `hbm_write_bytes > 0` for SBUF→HBM stores. These
  fields are populated regardless of DGE mode, engine, or opcode.

- **DMA transposes**: This investigation does not currently analyze DMA
  transposes. SBUF→SBUF transposes appear in DPA with `is_transpose_mode=True`
  and are excluded by the Step 2 query's `is_transpose_mode != True` filter.
  HBM→SBUF transposed loads may not be flagged (`is_transpose_mode` is NaN) so
  they may pass the filter and remain in Step 2 results.
