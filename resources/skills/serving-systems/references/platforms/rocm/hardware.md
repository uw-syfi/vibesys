# AMD Instinct MI300 family

Hardware spec reference. For the ROCm optimization floor see [`floor.md`](floor.md); for kernel-library guidance see [`aiter.md`](aiter.md).

## SKU matrix

| SKU | Architecture | HBM | HBM bandwidth | Interconnect |
|:----|:-------------|:----|:--------------|:-------------|
| MI300X | CDNA3 | 192 GB HBM3 | 5.3 TB/s | Infinity Fabric, 896 GB/s per-GPU bidir |
| MI325X | CDNA3 refresh | 256 GB HBM3e | 6.0 TB/s | Infinity Fabric |
| MI350X | CDNA4 (announced) | 288 GB HBM3e | TBD | Infinity Fabric |

Peak BF16 dense on MI300X: ~1.3 PFLOP/s; FP8 roughly doubles that.

## Compute capability (GFX ID)

- **gfx940 / gfx941 / gfx942** — CDNA3 (MI300 family)
- **gfx950** — CDNA4 (MI350, announced)

## Precision support

| Precision | CDNA3 | Notes |
|:----------|:------|:------|
| BF16 / FP16 | yes | |
| **FP8 (E4M3 / E5M2)** | yes (MI300 onward) | |
| INT8 | yes | |
| INT4 (via dequant paths) | software | |
| FP4 | CDNA4+ | |

## Topology

Typical MI300X node: **8 GPUs + Infinity Fabric mesh**. Pair bandwidth is lower than NVLink 4 on a DGX H100, but aggregate within-node bandwidth is comparable.

Beyond a node: Ethernet / InfiniBand with RDMA (RoCE). No NVL72-equivalent domain on current AMD systems.

## See also

- [`floor.md`](floor.md) — the ROCm optimization floor
- [`aiter.md`](aiter.md) — AITER / Composable Kernel, the fused-attention path on CDNA
- [`profiler.md`](profiler.md) — rocprof / omniperf
- [`roofline.md`](roofline.md): per-arch compute/bandwidth peaks and ridge points derived from these specs
