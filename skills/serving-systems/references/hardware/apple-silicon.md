# Apple Silicon (M-series)

Hardware spec reference for serving on Apple GPUs. For framework and software guidance, see [`frameworks/mlx/`](../frameworks/mlx.md).

## SKU overview

Apple doesn't publish GPU compute in standard TFLOP/s tables; the serving-relevant specs are GPU core count, unified memory capacity, and unified memory bandwidth.

| Chip | GPU cores | Memory bandwidth | Max unified memory |
|:-----|:----------|:-----------------|:-------------------|
| M1 / M1 Pro / Max / Ultra | up to 64 | 200–800 GB/s | up to 128 GB |
| M2 / M2 Pro / Max / Ultra | up to 76 | 200–800 GB/s | up to 192 GB |
| M3 / M3 Pro / Max | up to 40 | 200–400 GB/s | up to 128 GB |
| M4 / M4 Pro / Max | up to 40 | 250–546 GB/s | up to 128 GB |
| M2 Ultra / M4 Max (serving-oriented) | — | 546–800 GB/s | 128–192 GB |

Peak memory bandwidth is well below NVIDIA HBM; capacity per dollar is favorable for serving large models locally.

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

- [`frameworks/mlx/`](../frameworks/mlx.md) — the MLX framework paired with this hardware
