# Investigation: Redundant DMA transfers 

## Context

This investigation follows from a large `memory_bound_ideal → memory_bound_ideal_no_reloads`
gap in the [performance bounds](../performance-bounds.md). That gap measures
the cost — at peak bandwidth — of transferring more data from HBM than the algorithm
requires. At the kernel's actual (lower) bandwidth, the real cost is proportionally larger. 


The bounds identify that excess traffic exists but not the cause. The excess bytes could
be from:

- **Input reloads** — the same input data loaded from HBM multiple times because it was
  evicted from SBUF between uses
- **Intermediate spills** — computed data spilled to HBM and reloaded because SBUF
  couldn't hold it alongside other live data

This investigation quantifies the excess traffic, decomposes it by source, and traces
it to specific NKI source lines. DMA transposes from SBUF to SBUF can also contribute
 excess redundant work but are not yet covered.  

## Prerequisites

- Profile captured with `NEURON_RT_ENABLE_DGE_NOTIFICATIONS=1` (produces the
  `DmaPacketAggregated` table)
- NEFF compiled with `NEURON_FRAMEWORK_DEBUG=1` (produces `bir_debug_info_source_location`
  on instructions)

## Step 1: Detect — quantify excess HBM traffic and decompose by source

### Setup

```python
import pandas as pd, numpy as np, os

NE = os.path.expanduser("~/.local/share/neuron-profile/profiles/global")
profile = "blocking-tiled"  # replace with your profile name
d  = f"{NE}/{profile}@latest"

dma_pkts = pd.read_parquet(f"{d}/DmaPacket.parquet")
tensors = pd.read_parquet(f"{d}/TensorInfo.parquet")
metadata = pd.read_parquet(f"{d}/Metadata.parquet").iloc[0]

dma_bw_peak = metadata['dma_ddr_bandwidth']

# Kernel DMA packets (excludes infrastructure)
kernel_pkts = dma_pkts[(dma_pkts['queue_type'] != 'instruction')
                       & (dma_pkts['transfer_bytes'] > 4)]

# Time window
t0 = int(kernel_pkts['start_ts'].min())
t1 = int(kernel_pkts['end_ts'].max())
```

### Total bytes transferred vs necessary

```python
window_pkts = kernel_pkts[(kernel_pkts['start_ts'] >= t0) & (kernel_pkts['start_ts'] < t1)]
dma_transfer_bytes = window_pkts['transfer_bytes'].sum()

necessary_bytes = tensors[tensors['type'].isin(['IN', 'OUT'])]['size'].sum()

excess_bytes = dma_transfer_bytes - necessary_bytes
excess_ratio = dma_transfer_bytes / necessary_bytes

print(f"dma_transfer_bytes: {dma_transfer_bytes / 1e6:>8.0f} MB")
print(f"necessary_bytes:    {necessary_bytes / 1e6:>8.0f} MB")
print(f"excess_ratio:       {excess_ratio:>8.1f}x")
```

`dma_transfer_bytes` is the total data moved across all kernel DMA packets.
`necessary_bytes` is the sum of all input and output tensor sizes from
`TensorInfo` — the minimum if each tensor were transferred exactly once.
An `excess_ratio` of 1.0 means no excess. 

If your kernel algorithmically doesn't use the full input and output 
tensors, this will overestimate necessary bytes. Make a note of this and 
replace with your actual necessary bytes if this is the case (most likely, 
it's not!) 

### Decompose excess: reloads vs spills

The excess bytes have two possible sources. Separate them before deciding
which investigation to pursue.

```python
# Spill traffic: compiler-inserted SBUF <-> HBM evictions
spill_bytes = (
    dma_agg[dma_agg['dest'].str.contains('VIRTUAL', na=False)]['transfer_bytes'].sum()
    + dma_agg[dma_agg['source'].str.contains('VIRTUAL', na=False)]['transfer_bytes'].sum()
)

# Per-tensor breakdown
by_var = dma_agg.groupby('variable').agg(
    total_bytes=('transfer_bytes', 'sum'),
)

reload_excess = 0
for var, row in by_var.iterrows():
    ti = tensors[tensors['variable_name'] == var]
    if ti.empty:
        continue
    tensor_type = ti['type'].iloc[0]
    tensor_size = ti['size'].iloc[0]
    repeat = row['total_bytes'] / tensor_size

    if tensor_type == 'IN' and repeat > 1.0:
        reload_excess += row['total_bytes'] - tensor_size

    marker = " <<<" if tensor_type == 'IN' and repeat > 1.5 else ""
    print(f"  {var:<12} type={tensor_type:<4} size={tensor_size/1e6:>7.1f} MB  "
          f"xfer={row['total_bytes']/1e6:>8.1f} MB  repeat={repeat:>5.1f}x{marker}")

reload_pct = reload_excess / excess_bytes * 100 if excess_bytes > 0 else 0
spill_pct  = spill_bytes   / excess_bytes * 100 if excess_bytes > 0 else 0

print(f"\nreload_excess:  {reload_excess / 1e6:>8.0f} MB  ({reload_pct:.0f}% of excess)")
print(f"spill_bytes:    {spill_bytes / 1e6:>8.0f} MB  ({spill_pct:.0f}% of excess)")
```

**Spill traffic**: transfers where `source` or `dest` contains `[VIRTUAL]` —
compiler-inserted SBUF-HBM evictions. These appear in `DmaPacketAggregated`
but not in `TensorInfo`.

**Reload traffic**: input tensors (`type = 'IN'` in `TensorInfo`) loaded more
than once. The per-tensor excess is `total_bytes - tensor_size`. Tensors not
in `TensorInfo` (spill buffers) are skipped.


## Step 2a: Localize reloads — which source lines load which tensors?

Run this step if `reload_excess` dominates excess traffic in Step 1.

Join Instruction (source line) → Flow → DmaPacketAggregated
(tensor name, transfer bytes). Filter to input loads only.

```python
inst = pd.read_parquet(f"{d}/Instruction.parquet")
flow = pd.read_parquet(f"{d}/Flow.parquet")

# Flow rows linking instructions to their DMA transfers
inst_to_agg = flow[(flow['in_table'] == 'Instruction')
                   & (flow['out_table'] == 'DmaPacketAggregated')]

# HBM-reading instructions carry source attribution regardless of DGE mode
hbm_loads = inst[(inst['hbm_read_bytes'] > 0)
                 & (inst['bir_debug_info_source_location'].notna())].copy()
hbm_loads['src'] = hbm_loads['bir_debug_info_source_location'].apply(
    lambda x: x.split('/')[-1] if pd.notna(x) else '?')

joined = inst_to_agg.merge(
    hbm_loads[['id', 'src']].rename(columns={'id': 'in_id'}),
    on='in_id', how='inner')
joined = joined.merge(
    dma_agg[['id', 'variable', 'transfer_bytes', 'source']].rename(
        columns={'id': 'out_id'}),
    on='out_id', how='inner')

# Input loads only — excludes stores and compiler-inserted spill DMAs
input_loads = joined[joined['source'] == '[[INPUT]]']

by_src_var = input_loads.groupby(['src', 'variable']).agg(
    transfers=('transfer_bytes', 'count'),
    total_mb=('transfer_bytes', lambda x: x.sum() / 1e6),
).sort_values('total_mb', ascending=False)

for (src, var), row in by_src_var.iterrows():
    print(f"  {src:<45} {var:<12} xfers={int(row['transfers']):>6}  {row['total_mb']:>8.1f} MB")
```

Each row is a (source line, tensor) pair: which `dma_copy` call loads which
tensor, how many times, and how many bytes. Rank by `total_mb` to find the
dominant source of reload traffic.

The `source == '[[INPUT]]'` filter on DmaPacketAggregated excludes output
stores (`[[SB]]` → `[OUTPUT]`) and compiler-inserted spill DMAs
(`[[SB]]` → `[VIRTUAL]`, `[[VIRTUAL]]` → `[SB]`).

## Step 2b: Localize spills — which NKI operations cause spills?

Run this step if `spill_bytes` dominates excess traffic in Step 1.

Spill DMAs are compiler-inserted — they don't correspond to any `nisa.dma_copy`
in the NKI source. But their triggering instructions carry
`bir_debug_info_source_location` pointing to the NKI operation whose output was
spilled. Trace through `Flow.DGE_TRIGGER` to link spill transfers back to source
lines with byte attribution.

```python
inst = pd.read_parquet(f"{d}/Instruction.parquet")
flow = pd.read_parquet(f"{d}/Flow.parquet")

# Flow rows linking instructions to their DMA transfers
inst_to_agg = flow[(flow['in_table'] == 'Instruction')
                   & (flow['out_table'] == 'DmaPacketAggregated')]

spill_reloads = dma_agg[dma_agg['source'].str.contains('VIRTUAL', na=False)]
reload_flow = inst_to_agg[inst_to_agg['out_id'].isin(set(spill_reloads['id']))]

joined = reload_flow.merge(
    inst[['id', 'bir_debug_info_source_location']].rename(columns={'id': 'in_id'}),
    on='in_id', how='inner')
joined = joined.merge(
    spill_reloads[['id', 'transfer_bytes']].rename(columns={'id': 'out_id'}),
    on='out_id', how='inner')

joined['src'] = joined['bir_debug_info_source_location'].apply(
    lambda x: x.split('/')[-1] if pd.notna(x) else '?')

by_src = joined.groupby('src').agg(
    reloads=('transfer_bytes', 'count'),
    reload_mb=('transfer_bytes', lambda x: x.sum() / 1e6),
).sort_values('reload_mb', ascending=False)

by_src['spill_cost_mb'] = by_src['reload_mb'] * 2
total_spill_mb = spill_reloads['transfer_bytes'].sum() / 1e6 * 2

for src, row in by_src.iterrows():
    pct = row['spill_cost_mb'] / total_spill_mb * 100
    print(f"  {src:<45} reloads={int(row['reloads']):>4}  "
          f"spill_cost={row['spill_cost_mb']:>7.1f} MB  ({pct:.0f}%)")
```

Each row is a source line: the NKI operation whose output the compiler spilled.
`spill_cost` is the full round-trip cost (save + reload) attributed to that line.
Rank by `spill_cost_mb` to find the dominant contributor.


## Known issues

- **`TensorInfo.load_to_sbuf_repeat_factor`** (and `load_to_sbuf_dma_count`,
  `load_to_sbuf_total_size_bytes`, `load_to_sbuf_avg_size_bytes`): Schema intends
  these to give per-tensor reload metrics directly. Currently NULL for all NKI
  kernels (dynamic DMA path). Compute manually from `DmaPacketAggregated` as
  shown in Step 1.

- **DGE mode may affect available tables**: Depending on the NKI and
  neuronx-cc version, the `DmaPacketAggregated` table and the
  Instruction→DmaPacketAggregated flow link may only be produced for
  `dge_mode=hwdge` and `dge_mode=none`. Profiles using `dge_mode=swdge`
  (or `unknown` when the compiler selects swdge) may lack both — if so,
  Steps 2a and 2b cannot run. Additionally, if input load DPA rows are
  missing, the reload vs spill decomposition in Step 1 will be
  incomplete: reload_excess may report 0% even when reloads are present.
  Spill detection and Step 1's total excess (`dma_transfer_bytes` from
  DmaPacket) are unaffected.

- **Instruction→DPA flow edges may be incomplete**: A small number of
  Instruction or DmaPacketAggregated rows may lack a corresponding flow
  edge. Step 2a/2b results may be slightly incomplete as a result.
  Step 1 totals are unaffected since they use DPA directly.

- **Spill save-direction flow edges have partial coverage**: Step 2b
  uses the reload direction (VIRTUAL→SB) because save-direction
  (SB→VIRTUAL) flow edges may be missing for 5-40% of spill transfers.
  Since each buffer is saved and reloaded for identical bytes,
  `spill_cost = 2 × reload_bytes`.

