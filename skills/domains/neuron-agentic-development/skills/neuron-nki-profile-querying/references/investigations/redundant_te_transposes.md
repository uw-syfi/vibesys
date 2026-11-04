# Investigation: Redundant TE transposes

## Context

This investigation follows from a non-zero
`compute_bound_ideal_flops → compute_bound_ideal_useful_flops` gap in the
[performance bounds](../performance-bounds.md). That gap measures
TensorE FLOPs spent on `nc_transpose` operations — implemented as TRANSPOSE
MATMULs that consume hardware FLOPs without producing useful computation.

This investigation quantifies the transpose FLOPs overhead at peak throughput
and traces it to specific NKI source lines.

Note that there may be more redundant TE instructions like unnecessary computation
but this is not as easily / objectively quantifiable so the gap doesn't include them. 

## Prerequisites

- NEFF compiled with `NEURON_FRAMEWORK_DEBUG=1` (produces `bir_debug_info_source_location`
  on instructions)

## Step 1: Detect — quantify transpose FLOPs and the gap

### Setup

```python
import pandas as pd, numpy as np, os

NE = os.path.expanduser("~/.local/share/neuron-profile/profiles/global")
profile = "tp-with_transpose"  # replace with your profile name
d  = f"{NE}/{profile}@latest"

inst     = pd.read_parquet(f"{d}/Instruction.parquet")
dma_pkts = pd.read_parquet(f"{d}/DmaPacket.parquet")
metadata = pd.read_parquet(f"{d}/Metadata.parquet").iloc[0]


te_peak_flops = (metadata['tensor_engine_num_rows']
                 * metadata['tensor_engine_num_cols']
                 * 2.0 * metadata['tensor_engine_clock_freq'] * 1e9)

# Kernel DMA packets (excludes infrastructure)
kernel_pkts = dma_pkts[(dma_pkts['queue_type'] != 'instruction')
                       & (dma_pkts['transfer_bytes'] > 4)]
```

### Time window

All queries scope to `[t0, t1]`. Derived from the filtered kernel DMA
packets to cover all transfer modes. Override `t0`/`t1` to zoom into a
specific phase (see [Zooming in](#zooming-in)).

```python
t0 = int(kernel_pkts['start_ts'].min())
t1 = int(kernel_pkts['end_ts'].max())
```

### Compute the gap

```python
matmuls = inst[(inst['engine'] == 'Tensor') & (inst['opcode'] == 'MATMUL')
               & (inst['start_ts'] >= t0) & (inst['start_ts'] < t1)]

regular   = matmuls[matmuls['tensor_instruction_type'] == 'REGULAR']
transpose = matmuls[matmuls['tensor_instruction_type'] == 'TRANSPOSE']

hw_flops        = matmuls['adjusted_flops'].sum()
transpose_flops = transpose['adjusted_flops'].sum()
useful_flops    = hw_flops - transpose_flops
transpose_pct   = transpose_flops / hw_flops * 100 if hw_flops > 0 else 0
gap_us          = transpose_flops / te_peak_flops * 1e6

print(f"hw_flops:          {hw_flops / 1e9:>8.2f} G")
print(f"transpose_flops:   {transpose_flops / 1e9:>8.2f} G  ({transpose_pct:.1f}% of hw)")
print(f"useful_flops:      {useful_flops / 1e9:>8.2f} G")
print(f"TRANSPOSE MATMULs: {len(transpose):>8}")
print(f"REGULAR MATMULs:   {len(regular):>8}")
print(f"gap at peak TE:    {gap_us:>8.1f} us")
```

`transpose_flops` is the FLOPs consumed by TRANSPOSE MATMULs — real hardware
operations (identity matmuls with transposed layout) that do not contribute
to the kernel's mathematical result.

The gap — `transpose_flops / te_peak_flops` — is the minimum TensorE time
these transposes would take at peak throughput.

If `transpose_flops == 0`, the kernel has no on-device transposes. Stop here.

### Zooming in

Override `t0` and `t1`, then re-run the query above. Useful when:

- Transposes are concentrated in one phase (e.g., hoisted outside an inner
  loop) and you want the transpose fraction within that phase only
- A multi-phase kernel has transposes in one phase but not others
- You want to confirm whether transposes overlap with or are serialized
  against regular MATMULs in a specific window

Example — isolate the transpose phase of the first m-block:
```python
tp_sorted = transpose.sort_values('start_ts')
t0 = int(tp_sorted.iloc[0]['start_ts'])
t1 = int(tp_sorted.iloc[15]['end_ts'])  # first 16 transposes
# Re-run the Step 1 query with this t0, t1
```


## Step 2: Localize — which source lines produce the transposes?

This operates on the `transpose` DataFrame from Step 1. If Step 1 used a
`[t0, t1]` window, Step 2 automatically scopes to the same window.

```python
transpose_by_src = transpose.copy()
transpose_by_src['src'] = transpose_by_src['bir_debug_info_source_location'].apply(
    lambda x: x.split('/')[-1] if pd.notna(x) else '?')

by_src = transpose_by_src.groupby('src').agg(
    count=('adjusted_flops', 'count'),
    flops_g=('adjusted_flops', lambda x: x.sum() / 1e9),
    gap_us=('adjusted_flops', lambda x: x.sum() / te_peak_flops * 1e6),
).sort_values('flops_g', ascending=False)

by_src['pct'] = by_src['flops_g'] / (transpose_flops / 1e9) * 100

for src, row in by_src.iterrows():
    print(f"  {src}")
    print(f"    count={int(row['count'])}, flops={row['flops_g']:.2f} G, "
          f"gap={row['gap_us']:.1f} us ({row['pct']:.0f}% of transpose overhead)")
```

Each group corresponds to an `nisa.nc_transpose` call in the NKI source. Rank
by `flops_g` to identify which transpose call contributes most to the gap.


## Worked example

### The kernels

A 2048x2048x2048 bf16 matmul in two versions:

**V0 (with transpose):** takes A[M,K] and transposes each tile on-device
via `nc_transpose` before matmul:

```python
for m in nl.affine_range(M // TILE_M):          # 16
    for n in nl.affine_range(N // TILE_N):      # 4
        for k in nl.affine_range(K // TILE_K):  # 16
            nisa.dma_copy(dst=A_tile, src=A[m..., k...])
            nisa.nc_transpose(dst=A_t_psum, data=A_tile)    # line 48
            nisa.tensor_copy(dst=lhsT_tile, src=A_t_psum)
            nisa.dma_copy(dst=rhs_tile, src=rhs[k..., n...])
            nisa.nc_matmul(dst=acc, stationary=lhsT_tile, moving=rhs_tile)
```

**V1 (pre-transposed):** takes lhsT[K,M] already transposed, eliminates
`nc_transpose` entirely.

### Step 1

| Metric | V0 | V1 |
|--------|----|----|
| hw\_flops | 19.33 G | 17.18 G |
| transpose\_flops | 2.15 G (11.1% of hw) | 0 |
| useful\_flops | 17.18 G | 17.18 G |
| TRANSPOSE MATMULs | 1,024 | 0 |
| REGULAR MATMULs | 1,024 | 1,024 |
| gap at peak TE | 27.3 us | 0 |

V0 has equal TRANSPOSE and REGULAR MATMULs — one `nc_transpose` per
`nc_matmul`. The 2.15 G of transpose FLOPs accounts for 11.1% of total
hardware FLOPs. V1 eliminates all transposes; `useful_flops` is unchanged.

### Step 2

| Source line | count | GFLOPS | gap (us) | % of transpose |
|-------------|-------|--------|----------|----------------|
| v0\_with\_transpose.py:34 | 1,024 | 2.15 | 27.3 | 100% |

All 1,024 TRANSPOSE MATMULs in V0 come from the `nisa.nc_transpose` call
at line 34. V1 has no transposes — Step 2 produces no output.

### Bounds comparison

| Bound | V0 (us) | V1 (us) | Change |
|-------|---------|---------|--------|
| total\_time | 802 | 608 | -194 us (1.3x) |
| compute\_bound (TE active) | 503 | 420 | -83 us |
| compute\_bound\_ideal | 246 | 218 | -28 us |
| compute\_bound\_ideal\_useful | 218 | 218 | — |

`compute_bound_ideal` equals `compute_bound_ideal_useful` in V1 — all
TensorE FLOPs are useful. Total kernel time reduced by 194 us (24%).


## Known issues

- **`Summary.transpose_flops`**: Returns NaN when 0 transposes exist
  (e.g., pretransposed kernels) — use `fillna(0)` before comparing. Can
  be used as a quick check before running the full Step 1 query.
