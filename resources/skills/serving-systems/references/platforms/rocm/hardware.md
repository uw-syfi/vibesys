# AMD Instinct MI300 family

Hardware spec reference. For the ROCm optimization floor see [`floor.md`](floor.md); for kernel-library guidance see [`aiter.md`](aiter.md).

## Per-SKU reference: gfx target, CUs, memory model, FP4

| SKU | gfx target | CDNA generation | Compute units | Memory per device | Memory model | FP4 tensor support | Notes |
|:--|:--|:--|:--|:--|:--|:--|:--|
| MI300A | gfx942 | CDNA3 | 228 | 128 GB HBM3 per socket, ~5.3 TB/s | Unified (APU): host and device share one HBM pool | No | 4-socket node shows ~501 to 513 GB total visible (observed). Page cache competes with resident weights and KV/Mamba allocation; see [`unified-memory.md`](unified-memory.md). |
| MI300X | gfx942 | CDNA3 | 304 | 192 GB HBM3 | Discrete | No | Same ISA as MI300A; the unified-memory findings in this tree do not apply (discrete host/device memory, no page-cache contention). |
| MI350X / MI355X | gfx950 | CDNA4 | 256 | 288 GB HBM3e (public spec) | Discrete | Yes, native MXFP4 compute | AITER's CK a4w4 2-stage GEMM and MXFP4 MoE quant+sort target this generation; gfx942 lacks this path, see [`aiter.md`](aiter.md). |

Compute-unit count matters beyond raw throughput: aiter's tuned-GEMM lookup table is keyed in part on `cu_num`, so a config tuned on one SKU's CU count does not match another's even at the same `gfx` target (see the tuned-GEMM pitfall in [`aiter.md`](aiter.md)).

Scope: SKU-level, gfx942 and gfx950. Status: verified (gfx target, CDNA generation, CU counts, and MI300X/MI350X capacity are public spec; MI300A capacity-per-socket and the 5.3 TB/s figure are public spec; the ~501 to 513 GB node total is on-node observation). Stamp: public AMD spec (MI300 series, MI350 series) plus `sglang-v0.5.18-rocm700-mi30x`, 2026-08-25 to 2026-09-11 for the MI300A node-total observation and the CU-count mismatch consequence.

## Unified memory on MI300A

MI300A is an APU: host and device memory form one HBM pool, so page cache competes with resident model weights and with KV-cache and Mamba-cache allocation at scheduler init. This does not apply to MI300X (discrete). See [`unified-memory.md`](unified-memory.md) for the checkpoint load-path recipe, the KV-pool pinning fix, and the AITER mem-fraction interaction.

## SKU matrix

| SKU | Architecture | HBM | HBM bandwidth | Interconnect |
|:----|:-------------|:----|:--------------|:-------------|
| MI300X | CDNA3 | 192 GB HBM3 | 5.3 TB/s | Infinity Fabric, 896 GB/s per-GPU bidir |
| MI325X | CDNA3 refresh | 256 GB HBM3e | 6.0 TB/s | Infinity Fabric |
| MI350X | CDNA4 (announced) | 288 GB HBM3e | TBD | Infinity Fabric |

Peak BF16 dense on MI300X: ~1.3 PFLOP/s; FP8 roughly doubles that.

## Compute capability (GFX ID)

- **gfx940 / gfx941 / gfx942**: CDNA3 (MI300 family)
- **gfx950**: CDNA4 (MI350, announced)

## Precision support

| Precision | CDNA3 | Notes |
|:----------|:------|:------|
| BF16 / FP16 | yes | |
| **FP8 (E4M3 / E5M2)** | yes (MI300 onward) | |
| INT8 | yes | |
| INT4 (via dequant paths) | software | |
| FP4 | CDNA4+ | |

## ISA note: small in-kernel lookup tables

`v_perm_b32` (`__builtin_amdgcn_perm`) is gfx9's register-resident byte-select primitive for small per-lane lookup tables (a handful of bytes, compile-time-immediate selector, no memory access); a plain runtime-indexed array over the same table compiles to a compare-select chain instead and can cost more VALU issue than the load it was meant to replace, see [`aiter-mxfp4-moe.md`](aiter-mxfp4-moe.md).

## Topology

Typical MI300X node: **8 GPUs + Infinity Fabric mesh**. Pair bandwidth is lower than NVLink 4 on a DGX H100, but aggregate within-node bandwidth is comparable.

Beyond a node: Ethernet / InfiniBand with RDMA (RoCE). No NVL72-equivalent domain on current AMD systems.

## See also

- [`floor.md`](floor.md): the ROCm optimization floor
- [`aiter.md`](aiter.md): AITER / Composable Kernel, the fused-attention path on CDNA
- [`profiler.md`](profiler.md): rocprof / omniperf
