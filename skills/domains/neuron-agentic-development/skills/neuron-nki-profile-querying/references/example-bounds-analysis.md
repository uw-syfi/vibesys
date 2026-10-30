# Profile Analysis: blocking-tiled matmul

## Kernel (V0) 

A 4096x4096x4096 bf16 matmul: `C[M,N] = A^T[K,M] @ B[K,N]`. Input `lhsT` is
pre-transposed to [K, M]. Tiles into 128x128 stationary and 128x512 moving tiles.
Three nested loops (m=32, n=8, k=32) with two dma_copy calls and one nc_matmul
per inner iteration. 32 x 8 x 32 = 8,192 iterations.

```python
@nki.jit
def matmul_tiled(lhsT, rhs):
    K, M = lhsT.shape          # 4096, 4096
    K_, N = rhs.shape           # 4096, 4096
    TILE_M, TILE_K, TILE_N = 128, 128, 512

    result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

    for m in nl.affine_range(M // TILE_M):          # 32
        for n in nl.affine_range(N // TILE_N):      # 8
            res_psum = nl.ndarray((TILE_M, TILE_N), nl.float32, buffer=nl.psum)

            for k in nl.affine_range(K // TILE_K):  # 32
                lhsT_tile = nl.ndarray((TILE_K, TILE_M), ...)
                rhs_tile  = nl.ndarray((TILE_K, TILE_N), ...)
                nisa.dma_copy(dst=lhsT_tile, src=lhsT[k..., m...])
                nisa.dma_copy(dst=rhs_tile,  src=rhs[k..., n...])
                nisa.nc_matmul(dst=res_psum, stationary=lhsT_tile, moving=rhs_tile)

            nisa.tensor_copy(dst=res_sb, src=res_psum)
            nisa.dma_copy(dst=result[m..., n...], src=res_sb)

    return result
```

Inputs: lhsT[4096,4096] bf16, rhs[4096,4096] bf16.
Output: result[4096,4096] bf16.
Minimum I/O: 3 x 4096 x 4096 x 2 bytes = 100,663,296 bytes.

## Performance Bounds

Hardware: trn2, dma_ddr_bandwidth = 435 GB/s, TE peak = 78.6 TFLOPS.

### Bounds

| Bound | Value (us) |
|-------|-----------|
| total_time | 8,602 |
| memory_bound | 7,906 |
| memory_bound_ideal | 2,776 |
| memory_bound_no_reloads | 231 |
| compute_bound | 4,806 |
| compute_bound_ideal | 1,748 |
| compute_bound_ideal_useful | 1,748 |
| perfect_pipeline (DMA) | 7,906 |

### Engine active times

| Engine | Active time |
|--------|------------|
| DMA | 7,906 us (91.9%) |
| Tensor | 4,806 us (55.9%) |
| Vector | 176 us (2.0%) |
| Scalar | 0 us |
| GpSimd | 0 us |

Bottleneck: DMA

### Memory family gaps

| Gap | Value (us) | % of total |
|-----|-----------|------------|
| DMA idle | 696 | 8.1% |
| DMA inefficiency | 5,130 | 59.6% |
| Excess traffic | 2,545 | 29.6% (91.7% of traffic) |

### Compute family gaps

| Gap | Value (us) | % of total |
|-----|-----------|------------|
| TE idle | 3,796 | 44.1% |
| TE underutil | 3,058 | 35.6% |
| Transpose | 0 | 0.0% (0.0% of Flops)|

### Summary

Although TE instructions seem to be inefficient, DMA is the clear bottleneck 
and transfer inefficiency + redundant transfers contribute (59.6% + 29.6% = 89%) of total 
kernel execution time. Eliminating this would lead to the next unaffected engine (TE) 
to become the bottleneck at 4,806ns (1.79x speedup). 

Investigations (Appendix V0): 
- Redundant dma transfers: Using the investigations/redundant_dma_transfers.md steps 
we find that all excess dma transfers are input reloads. Almost entirely  
dma transfers are input reloads. Almost entirely from reloading the rhs tensor. 
- DMA efficiency: Using the investigations/dma_efficiency.md steps, we find that loads and 
stores are also well below ideal transfer size. 

DMA is the bottleneck and it's execution is dominated by redundant input reloads, this is the gap
we will prioritize for V1 even if we keep in mind that transfer sizes should be increased in the future
as well. 

## V1: Blocking free dimension

V0 has three nested loops where both operands are (attempted to be) loaded on every 
inner iteration. To reduce the excessive input reloads, in V1, we block the loads over
the M and N dimension to localize computation. 

Key structural change from V0:

```python
TILES_IN_BLOCK_M = 12
BLOCK_M = TILES_IN_BLOCK_M * TILE_M  # 1536

for m_block in nl.affine_range(M // BLOCK_M):        # 3 M-blocks

    # Pre-load lhsT tiles for this M-block (reused across all N)
    for bm in nl.affine_range(TILES_IN_BLOCK_M):     # 12
        for k in nl.affine_range(K // TILE_K):        # 32
            nisa.dma_copy(dst=lhsT_tiles[bm][k],
                          src=lhsT[k..., m_block*BLOCK_M + bm*TILE_M...])

    for n in nl.affine_range(N // TILE_N):            # 8
        # Load rhs tiles once per (m_block, n) — reused across 12 m-tiles
        for k in nl.affine_range(K // TILE_K):        # 32
            nisa.dma_copy(dst=rhs_tiles[k],
                          src=rhs[k..., n...])

        # Compute: all bm x k matmuls
        for bm in nl.affine_range(TILES_IN_BLOCK_M):  # 12
            for k in nl.affine_range(K // TILE_K):     # 32
                nisa.nc_matmul(dst=res_psum[bm],
                               stationary=lhsT_tiles[bm][k],
                               moving=rhs_tiles[k])
```

The rhs repeat drops from 32x (once per m-tile) to 3x (once per M-block).
lhsT is loaded once per M-block and reused across all 8 N-tiles.

### V1 Bounds (BM=8, BN=2)

| Bound | V0 (us) | V1 (us) | V0 -> V1 |
|-------|---------|---------|--------|
| total_time | 8,602 | 2,451 | -6,151 us (3.5x) |
| memory_bound | 7,906 | 1,325 | -6,581 us (6.0x) |
| memory_bound_ideal | 2,776 | 463 | -2,313 us (6.0x) |
| memory_bound_no_reloads | 231 | 231 | — |
| compute_bound | 4,806 | 2,163 | -2,643 us (2.2x) |
| compute_bound_ideal | 1,748 | 1,748 | — |
| compute_bound_ideal_useful | 1,748 | 1,748 | — |
| perfect_pipeline | 7,906 (DMA) | 2,163 (Tensor) | -5,743 us (3.7x) |

### Engine active times

| Engine | V0 | V1 | V0 -> V1 |
|--------|----|----|----------|
| DMA | 7,906 us (91.9%) | 1,325 us (54.1%) | -6,581 us (6.0x) |
| Tensor | 4,806 us (55.9%) | 2,163 us (88.2%) | -2,643 us (2.2x) |
| Vector | 176 us (2.0%) | 174 us (7.1%) | — |
| Scalar | 0 us | 0 us | — |
| GpSimd | 0 us | 0 us | — |

Bottleneck: DMA → Tensor

### Memory family gaps

| Gap | V0 | V1 | V0 -> V1 |
|-----|----|----|----------|
| DMA idle | 696 us (8.1%) | 1,126 us (46.0%) | +430 us |
| DMA inefficiency | 5,130 us (59.6%) | 862 us (35.2%) | -4,268 us (6.0x) |
| Excess traffic | 2,545 us (91.7% of traffic) | 231 us (50.0% of traffic) | -2,314 us (11.0x) |

### Compute family gaps

| Gap | V0 | V1 | V0 -> V1 |
|-----|----|----|----------|
| TE idle | 3,796 us (44.1%) | 288 us (11.8%) | -3,508 us (13.2x) |
| TE underutil | 3,058 us (35.6%) | 416 us (17.0%) | -2,642 us (7.4x) |
| Transpose | 0 (0.0% of flops) | 0 (0.0% of flops) | — |

### Summary

V0 -> V1: Reducing input reloads reduced both the redundant transfers and dma efficiency
gap as expected. As DMA stopped being the bottleneck, DMA idle gaps also increased
as expected while TE idle gaps reduced as it became the new bottleneck. More interestingly, TE engine active time went down which seems to drive a conveniently reduced gap (less throttling). 

V1: Tensor Engine is the bottleneck at 2,163 us. TE idle (11.8%) and TE
underutilization (17.0%) together account for 28.8% of total time in the
bottleneck family. These gaps should be the new focus. 

Investigations (Appendix V1):
- TE idle gaps: Examining TE idle gaps tells us that the majority 
of TE idle spans are caused by waiting on dma transfers. 
- TE inefficiency: Running the investigations/te_inefficiency.md steps tells us that the tile
sizes are the right size and inefficiency seems to come from throttling due to idle gaps. 
- DMA efficiency: Running the investigations/dma_efficiency.md steps tells us that all transfers 
are well below the ideal threshold causing low dma BW utilization. 

Since TE engine is the bottleneck, and TE inefficiency seems to all stem from idle gaps specifically 
waiting on dma, this is the performance gap we will tackle. We will do this by improving dma efficiency
through larger transfers. We could also look at techniques for pipelining. 

## V2: Row loads

V1 loads individual tiles per DMA call (128x128 for lhsT, 128x512 for rhs).
V2 loads full block-width rows instead: 128x1024 for lhsT (one row covers
all 8 M-tiles in the block) and 128x1024 for rhs (covers 2 N-tiles). The
matmul slices into the pre-loaded rows in SBUF. Same total bytes, fewer
larger transfers.

```python
for m in nl.affine_range(M // BLOCK_M):
    # Row-load lhsT: one [TILE_K, BLOCK_M] = [128, 1024] per K tile
    lhsT_rows = []
    for k in nl.affine_range(num_k):
        row = nl.ndarray((TILE_K, BLOCK_M), ...)
        nisa.dma_copy(dst=row, src=lhsT[k..., m_block...])
        lhsT_rows.append(row)

    for n in nl.affine_range(N // BLOCK_N):
        # Row-load rhs: one [TILE_K, BLOCK_N] = [128, 1024] per K tile
        rhs_rows = []
        for k in nl.affine_range(num_k):
            row = nl.ndarray((TILE_K, BLOCK_N), ...)
            nisa.dma_copy(dst=row, src=rhs[k..., n_block...])
            rhs_rows.append(row)

        # Matmul: slice into rows
        for bm in nl.affine_range(TILES_IN_BLOCK_M):
            for bn in nl.affine_range(TILES_IN_BLOCK_N):
                for k in nl.affine_range(num_k):
                    nisa.nc_matmul(dst=accum,
                        stationary=lhsT_rows[k][..., bm*TILE_M:(bm+1)*TILE_M],
                        moving=rhs_rows[k][..., bn*TILE_N:(bn+1)*TILE_N])
```

### V2 Bounds (BM=8, BN=2, row loads)

| Bound | V0 (us) | V1 (us) | V2 (us) | V1 -> V2 |
|-------|---------|---------|---------|----------|
| total_time | 8,602 | 2,451 | 1,801 | -650 us (1.4x) |
| memory_bound | 7,906 | 1,325 | 819 | -506 us (1.6x) |
| memory_bound_ideal | 2,776 | 463 | 463 | — |
| memory_bound_no_reloads | 231 | 231 | 231 | — |
| compute_bound | 4,806 | 2,163 | 1,781 | -382 us (1.2x) |
| compute_bound_ideal | 1,748 | 1,748 | 1,748 | — |
| compute_bound_ideal_useful | 1,748 | 1,748 | 1,748 | — |
| perfect_pipeline | 7,906 (DMA) | 2,163 (Tensor) | 1,781 (Tensor) | -382 us (1.2x) |

### Engine active times

| Engine | V0 | V1 | V2 | V1 -> V2 |
|--------|----|----|----|----|
| DMA | 7,906 us (91.9%) | 1,325 us (54.1%) | 819 us (45.5%) | -506 us (1.6x) |
| Tensor | 4,806 us (55.9%) | 2,163 us (88.2%) | 1,781 us (98.9%) | -382 us (1.2x) |
| Vector | 176 us (2.0%) | 174 us (7.1%) | 172 us (9.6%) | — |
| Scalar | 0 us | 0 us | 0 us | — |
| GpSimd | 0 us | 0 us | 0 us | — |

Bottleneck: Tensor

### Memory family gaps

| Gap | V0 | V1 | V2 | V1 -> V2 |
|-----|----|----|----|----|
| DMA idle | 696 us (8.1%) | 1,126 us (46.0%) | 982 us (54.5%) | -144 us |
| DMA inefficiency | 5,130 us (59.6%) | 862 us (35.2%) | 356 us (19.8%) | -506 us (2.4x) |
| Excess traffic | 2,545 us (91.7% of traffic) | 231 us (50.0% of traffic) | 231 us (50.0% of traffic) | — |

### Compute family gaps

| Gap | V0 | V1 | V2 | V1 -> V2 |
|-----|----|----|----|----|
| TE idle | 3,796 us (44.1%) | 288 us (11.8%) | 20 us (1.1%) | -268 us (14.4x) |
| TE underutil | 3,058 us (35.6%) | 416 us (17.0%) | 33 us (1.8%) | -383 us (12.6x) |
| Transpose | 0 (0.0% of flops) | 0 (0.0% of flops) | 0 (0.0% of flops) | — |

### Summary

V1 -> V2: Row loads reduced DMA inefficiency from 862 to 356 us (achieved BW
from 151.9 to 245.8 GB/s). Same transfer bytes, fewer larger transfers. TE
gaps collapsed: idle from 288 to 20 us, underutil from 416 to 33 us. TE is
now running at near-peak utilization (compute_bound 1,781 vs compute_bound_ideal
1,748 — 33 us gap, 1.8%).

V2: Tensor Engine is the bottleneck at 1,781 us , operating at near-peak. Remaining overhead is minimal.

## Appendix: V0 Investigation — Redundant DMA (Step 1)

### Excess traffic decomposition

| Metric | Value |
|--------|-------|
| dma_transfer_bytes | 1,207,697,408 bytes |
| necessary_bytes | 100,663,296 bytes |
| excess_bytes | 1,107,034,112 bytes |
| excess_ratio | 12.0x |

### Per-tensor breakdown

| Tensor | Type | Size (bytes) | Transferred (bytes) | Repeat | Identity |
|--------|------|-------------|---------------------|--------|----------|
| input0 | IN | 33,554,432 | 1,073,741,824 | 32.0x | rhs (128x512 tiles) |
| input1 | IN | 33,554,432 | 100,532,224 | 3.0x | lhsT (128x128 tiles) |
| output0 | OUT | 33,554,432 | 33,423,360 | 1.0x | result |

Identity determined from transfer sizes: input0 transfers are 131,072 bytes
(128x512x2 = TILE_K x TILE_N), input1 transfers are 32,768 bytes
(128x128x2 = TILE_K x TILE_M).

### Decomposition

| Source | Bytes | % of excess |
|--------|-------|-------------|
| reload_excess | 1,107,165,184 | 100% |
| spill_bytes | 0 | 0% |

All excess traffic is input reloads. No spills. rhs (input0) is loaded 32x,
lhsT (input1) is loaded 3x.

## Appendix: V1 Investigations

V1 is Tensor-bottlenecked. Relevant groups: 2a (TE idle), 2b (TE underutil).

### Investigation 2b: Compute Tile Sizes (Step 1 & 2)

####
| Metric | Value |
|--------|-------|
| TE peak | 78.6 TFLOPS |
| Achieved | 63.5 TFLOPS (81%) |

| Source line | Tiles | % of hw_flops | K/128 | M/128 | N/512 |
|-------------|-------|---------------|-------|-------|-------|
| run_blocking_v1.py:70 | 8,192 | 100% | 128/128 | 128/128 | 512/512 |

All tile dimensions are at maximum. The 19% gap between achieved and peak
is not from undersized tiles.

### Investigation 2a: DMA-Compute Pipelining (Step 1)

| Metric | Value |
|--------|-------|
| REGULAR MATMULs | 8,192 |
| total excess initiation interval | 683 us |
| DMA-caused excess | 95 us (14% of excess) |

| Last-finishing dependency | Count |
|--------------------------|-------|
| Tensor (previous MATMUL) | 7,778 |
| DPA (DMA transfer) | 258 |
| Vector | 155 |

95% of MATMULs have their previous MATMUL as the last-finishing dependency —
the pipeline is running normally. Only 258 MATMULs (3%) are DMA-gated.
The 683 us total excess is mostly from TE-to-TE initiation overhead, not
DMA starvation.
