# Apple Silicon (M-series)

Hardware spec reference for serving on Apple GPUs. For framework and software guidance see [`mlx.md`](mlx.md); for the optimization floor see [`floor.md`](floor.md).

## The number that matters: memory bandwidth

Decode on this backend is bandwidth-bound at batch 1, so bandwidth predicts tokens/sec more directly than any other spec. Establish it before designing.

```
tokens/sec ≈ memory_bandwidth / bytes_per_token
bytes_per_token ≈ model_size_bytes        (weight-streaming dominated, batch 1)
```

A 7B model at INT4 is roughly 3.5 GB per token streamed. At 400 GB/s that ceiling is ~114 tok/s; at 800 GB/s, ~228. Measured throughput close to that bound means kernel work will not help — reduce bytes per token instead ([`floor.md`](floor.md) §5).

The spread across the line is roughly 4× — the single largest determinant of serving performance on Apple Silicon, and the reason a base-tier chip and an Ultra are not the same target.

## SKU overview

Apple doesn't publish GPU compute in standard TFLOP/s tables; the serving-relevant specs are unified memory bandwidth, unified memory capacity, and GPU core count — in that order.

| Chip tier | Memory bandwidth | Max unified memory | GPU cores |
|:-----|:-----------------|:-------------------|:----------|
| Base (M1 / M2 / M3 / M4) | ~100–120 GB/s | 16–32 GB | 8–10 |
| Pro | ~150–273 GB/s | 18–64 GB | 14–20 |
| Max | ~300–546 GB/s | 32–128 GB | 30–40 |
| Ultra (M1 / M2) | ~800 GB/s | 128–192 GB | 60–76 |
| Ultra (M3) | ~819 GB/s | up to **512 GB** | up to 80 |

Treat these as tiers rather than exact per-model figures — bandwidth varies within a tier across generations, and the tier is what determines whether a given model is servable. Confirm the exact figure for the target machine.

Peak bandwidth is well below NVIDIA HBM; capacity per dollar is favorable for serving large models locally, which is the niche this backend occupies.

## Unified memory model

- CPU and GPU share one physical DRAM pool.
- No explicit host ↔ device transfer; no `.to("cuda")` equivalent with copy cost.
- Memory is contended with the OS and every running app — not a dedicated GPU pool.
- Swap kicks in before physical memory fills; plan ~25% headroom.

## Compute features

- Neural Engine (ANE) — separate accelerator for small / low-precision models; not the GPU path.
- **Matrix engine / AMX** — per-cluster matrix unit (private ABI; used via Accelerate / MLX).
- **Metal GPU shaders** — programmable compute units.
- M3 / M4 add improved matrix units; exact throughput numbers are not published.

## Precision support

| Precision | Support |
|:----------|:--------|
| FP32 / FP16 | yes |
| BF16 | yes on newer chips (M3 / M4); partial on earlier |
| INT8 / INT4 | yes (via MLX / GGUF); no tensor-core equivalent |
| FP8 / FP4 | no |

## Topology

One GPU per SoC. No multi-GPU configurations (no NVLink equivalent, no second-GPU PCIe slot). Multi-machine serving across Macs is possible but unusual.

## See also

- [`floor.md`](floor.md) — the Apple Silicon optimization floor
- [`mlx.md`](mlx.md) — the MLX framework paired with this hardware
- [`mlx-serving.md`](mlx-serving.md) — the serving loop
- [`profiler.md`](profiler.md) — Instruments, and the roofline check
