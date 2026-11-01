# Investigation: Compute engine (TE) inefficiency

## Context

This investigation follows from a large
`compute_bound → compute_bound_ideal_flops` gap in the
[performance bounds](../performance-bounds.md). That gap
measures TensorE active time not producing FLOPs at peak rate — the
hardware is executing but underutilized. We will investigate tile sizes of TE
matmul instructions in this investigation but not all the gap may be 
explained. In some cases of poor utilization or poor
pipelining, throttling can inflate instruction durations.

This investigation extracts the tile dimensions (K, M, N) from each
MATMUL instruction and identifies source lines that use tiles smaller
than the hardware maximums (K=128, M=128, N=512).

## Prerequisites

- NEFF compiled with `NEURON_FRAMEWORK_DEBUG=1` (produces
  `bir_debug_info_source_location` on instructions)

## Step 1: Detect — quantify the compute efficiency gap

### Setup

```python
import pandas as pd, numpy as np, os, re

NE = os.path.expanduser("~/.local/share/neuron-profile/profiles/global")
profile = "my-kernel"  # replace with your profile name
d  = f"{NE}/{profile}@latest"

inst     = pd.read_parquet(f"{d}/Instruction.parquet")
active   = pd.read_parquet(f"{d}/ActiveTime.parquet")
dma_pkts = pd.read_parquet(f"{d}/DmaPacket.parquet")
metadata = pd.read_parquet(f"{d}/Metadata.parquet").iloc[0]

te_peak = (metadata['tensor_engine_num_rows']
           * metadata['tensor_engine_num_cols']
           * 2.0 * metadata['tensor_engine_clock_freq'] * 1e9)

# Kernel DMA packets (excludes infrastructure)
kernel_pkts = dma_pkts[(dma_pkts['queue_type'] != 'instruction')
                       & (dma_pkts['transfer_bytes'] > 4)]
```

### Interval merge primitive

```python
def interval_merge_ns(starts, ends, t0, t1):
    starts = np.maximum(starts, t0)
    ends   = np.minimum(ends, t1)
    valid  = starts < ends
    starts, ends = starts[valid], ends[valid]
    if len(starts) == 0:
        return 0
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
```

### Time window

```python
t0 = int(kernel_pkts['start_ts'].min())
t1 = int(kernel_pkts['end_ts'].max())
```

### Compute the gap

```python
matmuls = inst[(inst['engine'] == 'Tensor') & (inst['opcode'] == 'MATMUL')
               & (inst['start_ts'] >= t0) & (inst['start_ts'] < t1)]
hw_flops = matmuls['adjusted_flops'].sum()

te_active_ns = interval_merge_ns(
    active[active['engine'] == 'tensor']['start_ts'].values,
    active[active['engine'] == 'tensor']['end_ts'].values, t0, t1)

achieved_tflops = hw_flops / (te_active_ns / 1e9) / 1e12
utilization = achieved_tflops / (te_peak / 1e12)

print(f"TE peak:        {te_peak/1e12:.1f} TFLOPS")
print(f"Achieved:       {achieved_tflops:.1f} TFLOPS ({utilization:.0%})")
```

`adjusted_flops` normalizes each instruction to bf16-equivalent FLOPs
(1× for bf16/fp16/tf32, 4× for fp32, 0.5× for fp8 on trn2), matching
`te_peak` which is the bf16 peak. `utilization` is the fraction of TE
active time producing bf16-equivalent FLOPs at peak rate. 


## Step 2: Localize — which source lines use undersized tiles?

Extract K, M, N from the `operands` field on each MATMUL instruction.
The field encodes `K*M` as the trailing pair and N as the first element
of the `src` stride.

```python
def extract_matmul_dims(operands_str):
    km = re.search(r'(\d+)\*(\d+)\s*$', operands_str)
    if not km:
        return None, None, None
    K, M = int(km.group(1)), int(km.group(2))
    src = re.search(r'src=\S+\[[\d,\-]+\]\[(\d+),', operands_str)
    N = int(src.group(1)) if src else None
    return K, M, N

for src, grp in matmuls.groupby('bir_debug_info_source_location'):
    K, M, N = extract_matmul_dims(grp.iloc[0]['operands'])
    n = len(grp)
    total_flops = grp['adjusted_flops'].sum()
    pct = total_flops / hw_flops * 100

    print(f"  {src}")
    print(f"    tiles={n}, {pct:.0f}% of hw_flops")
    print(f"    K={K}/128  M={M}/128  N={N}/512")
```

Source lines where any dimension is below its maximum are producing fewer
FLOPs per tile than the hardware can sustain per initiation interval. M
and K underutilization reduce throughput linearly. N underutilization has
minimal impact on streaming throughput because the initiation interval
shortens proportionally.

Before pointing out a gap. Verify that the user is not doing complicated PE
tiling intentionally, this is not in scope. 

## Worked example

### The kernels

Both kernels load one stationary and one moving tile, then run 2048
`nc_matmul` calls. Same total matmul count, different tile dimensions.

**V0 (mixed tiles):** 4 sections of 512 matmuls each, one per undersized
dimension:

```python
stat_full = nl.ndarray((128, 128), ...)
mov_full  = nl.ndarray((128, 512), ...)
nisa.dma_copy(dst=stat_full, src=lhsT[0:128, 0:128])
nisa.dma_copy(dst=mov_full,  src=rhs[0:128, 0:512])

# Section 1: M=32
out1 = nl.ndarray((32, 512), ...)
for _ in nl.affine_range(512):
    nisa.nc_matmul(dst=out1, stationary=stat_full[0:128, 0:32], moving=mov_full)    # line 30

# Section 2: N=64
out2 = nl.ndarray((128, 64), ...)
for _ in nl.affine_range(512):
    nisa.nc_matmul(dst=out2, stationary=stat_full, moving=mov_full[0:128, 0:64])    # line 35

# Section 3: K=32
out3 = nl.ndarray((128, 512), ...)
for _ in nl.affine_range(512):
    nisa.nc_matmul(dst=out3, stationary=stat_full[0:32, 0:128], moving=mov_full[0:32, 0:512])  # line 40

# Section 4: Peak
out4 = nl.ndarray((128, 512), ...)
for _ in nl.affine_range(512):
    nisa.nc_matmul(dst=out4, stationary=stat_full, moving=mov_full)                 # line 45
```

**V1 (peak tiles):** 2048 matmuls all at K=128, M=128, N=512.

### Step 1

| Metric | V0 (mixed) | V1 (peak) |
|--------|-----------|-----------|
| TE peak | 78.6 TFLOPS | 78.6 TFLOPS |
| Achieved | 30.8 TFLOPS (39%) | 77.2 TFLOPS (98%) |
| MATMULs | 2,048 | 2,048 |

V0's overall utilization is 39% — a weighted average across the four
sections. V1 achieves 98% with all peak tiles.

### Step 2

**V0** — four source lines with different tile dimensions:

| Source line | Tiles | % of flops | K/128 | M/128 | N/512 | Undersized |
|-------------|-------|-----------|-------|-------|-------|------------|
| v0\_mixed\_tiles.py:30 | 512 | 15% | 128/128 | 32/128 | 512/512 | M |
| v0\_mixed\_tiles.py:35 | 512 | 8% | 128/128 | 128/128 | 64/512 | N |
| v0\_mixed\_tiles.py:40 | 512 | 15% | 32/128 | 128/128 | 512/512 | K |
| v0\_mixed\_tiles.py:45 | 512 | 62% | 128/128 | 128/128 | 512/512 | none |

**V1** — single source line, all peak:

| Source line | Tiles | % of flops | K/128 | M/128 | N/512 | Undersized |
|-------------|-------|-----------|-------|-------|-------|------------|
| v1\_peak.py:28 | 2,048 | 100% | 128/128 | 128/128 | 512/512 | none |

Each undersized section contributes 512 tiles but different fractions of
total FLOPs. The N=64 section produces only 8% of FLOPs (64/512 = 12.5%
of peak per tile). The peak section produces 62% despite being only 512
of 2048 tiles.


## Known issues

- **`Instruction.operands` format**: The `K*M` trailing pair and `src`
  stride encoding are observed on neuron-explorer 2.22+. The format is
  not documented and may change in future versions. Validate the
  extraction on a known tile size before relying on it.

- **TRANSPOSE MATMULs**: Both steps include TRANSPOSE MATMULs in the
  totals. Step 2 will show transpose source lines with their tile
  dimensions alongside regular matmuls. The transpose overhead itself
  is covered by the
  [transpose investigation](redundant_te_transposes.md).
