# NVIDIA GPUs for serving

Hardware spec reference across five generations. For backend / algorithm / kernel-choice guidance, see the relevant [`backends/*`](../../backends/) and [`algorithms/*`](../../algorithms/) skills.

## Generations at a glance

| Gen | Arch | Compute capability | Tensor cores | New precision | Year |
|:----|:-----|:-------------------|:-------------|:--------------|:-----|
| **Blackwell** | GB100 / GB102 / GB200 / GB202 | sm_100 / sm_100a / sm_103 / sm_110 / sm_120 | Gen 5 (tcgen05, block-scaled MMA) | **FP4** (NVFP4, MXFP4), MXFP8 | 2024– |
| **Hopper** | GH100 | sm_90 / sm_90a | Gen 4 (+ TMA, WGMMA) | **FP8** (E4M3, E5M2) | 2022–2024 |
| **Ada Lovelace** | AD102 / AD104 | sm_89 | Gen 4 | **FP8** (E4M3, E5M2) — Ada too | 2022 |
| **Ampere** | GA100 / GA102 | sm_80 / sm_86 | Gen 3 | **BF16**, **TF32** | 2020 |
| **Turing** | TU104 | sm_75 | Gen 2 | **INT8**, INT4 (FP16 only, no BF16) | 2018 |

The older the generation, the fewer fused / low-precision tensor-core paths apply. Below is the SKU matrix per generation, then the consolidated precision-support table.

## Blackwell SKUs

| SKU | Tier | Memory | Memory bandwidth | Interconnect | TDP |
|:----|:-----|:-------|:-----------------|:-------------|:----|
| **B200** (HGX SXM) | data-center | 192 GB HBM3e | ~8 TB/s | NVLink 5, 1.8 TB/s bidir | ~1000 W |
| **RTX PRO 6000 Blackwell** (Server / Workstation) | workstation + low-end server | 96 GB GDDR7 (ECC) | ~1.8 TB/s | PCIe 5.0 x16; NVLink 5 on Server edition | 300 W (server) / 600 W (workstation) |

Also in the Blackwell family but not requested: B100 (HGX), GB200 superchip (Grace + 2× Blackwell), B300 (288 GB HBM3e refresh).

Approximate dense B200 throughput: **~9 PFLOP/s FP4**, ~4.5 PFLOP/s FP8, ~2.25 PFLOP/s BF16. RTX PRO 6000 is ~8–10× smaller on compute but still ships the full tcgen05 + FP4 feature set; useful for on-prem inference without NVLink fabric.

## Hopper SKUs

| SKU | Tier | Memory | Memory bandwidth | Interconnect | TDP |
|:----|:-----|:-------|:-----------------|:-------------|:----|
| **H100 SXM5** | data-center | 80 GB HBM3 | 3.35 TB/s | NVLink 4, 900 GB/s bidir | 700 W |
| **H100 PCIe** | data-center | 80 GB HBM3 | 2.04 TB/s | NVLink 4 (reduced) | 350 W |
| **H200 SXM5** | data-center | 141 GB HBM3e | 4.8 TB/s | NVLink 4, 900 GB/s bidir | 700 W |

Dense throughput: **~1979 TFLOP/s FP8**, 989 TFLOP/s BF16. H200 has the same compute as H100; the difference is 1.76× capacity + 1.4× bandwidth. (H20 also exists — export-compliant with ~15% of H100 compute but similar HBM bandwidth; decode-friendly, prefill-punishing.)

## Ada Lovelace SKUs

| SKU | Tier | Memory | Memory bandwidth | Interconnect | TDP |
|:----|:-----|:-------|:-----------------|:-------------|:----|
| **L40S** | data-center (server Ada) | 48 GB GDDR6 (ECC) | 864 GB/s | PCIe 4.0 x16 | 350 W |
| **L4** | data-center (inference / edge) | 24 GB GDDR6 (ECC) | 300 GB/s | PCIe 4.0 x16 | 72 W |

**Ada has FP8 tensor cores** (Gen-4, same as Hopper) even though it's a consumer-die-derived architecture. L40S is the workhorse for quantized-LLM inference without HBM: ~362 TFLOP/s BF16 dense, ~733 TFLOP/s FP8 dense. L4 is ~1/3 of L40S on compute and ~1/3 on bandwidth at ~1/5 the power — well-suited for low-QPS deployments.

## Ampere SKUs

| SKU | Tier | Memory | Memory bandwidth | Interconnect | TDP |
|:----|:-----|:-------|:-----------------|:-------------|:----|
| **A100 40 GB SXM4** | data-center | 40 GB HBM2 | 1.555 TB/s | NVLink 3, 600 GB/s bidir | 400 W |
| **A100 40 GB PCIe** | data-center | 40 GB HBM2 | 1.555 TB/s | NVLink 3 (reduced) | 250 W |
| **A100 80 GB SXM4** | data-center | 80 GB HBM2e | 2.04 TB/s | NVLink 3, 600 GB/s bidir | 400 W |
| **A100 80 GB PCIe** | data-center | 80 GB HBM2e | 1.94 TB/s | NVLink 3 (reduced) | 300 W |
| **A10** | data-center (inference) | 24 GB GDDR6 | 600 GB/s | PCIe 4.0 x16 (no NVLink) | 150 W |

A100 was the first HBM data-center GPU at BF16 + TF32 tensor cores: ~312 TFLOP/s BF16 dense. No FP8, no FP4. A10 is a GDDR inference card with the same sm_86 Ampere tensor cores; serves smaller models well but has no NVLink — treated as a single-GPU inference endpoint.

## Turing SKUs

| SKU | Tier | Memory | Memory bandwidth | Interconnect | TDP |
|:----|:-----|:-------|:-----------------|:-------------|:----|
| **T4** | data-center (inference / edge) | 16 GB GDDR6 (ECC) | 320 GB/s | PCIe 3.0 x16 (no NVLink) | 70 W |

Turing's Gen-2 tensor cores are **FP16 / INT8 / INT4 only** — **no BF16, no FP8, no FP4**. Still deployed widely as an edge-inference card. For serving modern LLMs, expect to run FP16 (not BF16) and lean on INT8 / INT4 quantization.

## Precision support matrix

| Precision | Turing sm_75 | Ampere sm_80/86 | Ada sm_89 | Hopper sm_90 | Blackwell sm_100 |
|:----------|:-------------|:----------------|:----------|:-------------|:-----------------|
| FP16 | ✓ | ✓ | ✓ | ✓ | ✓ |
| **BF16** | — | ✓ | ✓ | ✓ | ✓ |
| **TF32** | — | ✓ | ✓ | ✓ | ✓ |
| INT8 | ✓ | ✓ | ✓ | ✓ | ✓ |
| INT4 | ✓ | ✓ | ✓ | ✓ | ✓ |
| **FP8 E4M3 / E5M2** | — | — | ✓ | ✓ | ✓ |
| **NVFP4** | — | — | — | — | ✓ |
| **MXFP4** (OCP microscaling) | — | — | — | — | ✓ |
| **MXFP8** | — | — | — | — | ✓ |
| Block-scaled MMA | — | — | — | — | ✓ |
| Sparse 2:4 tensor cores | — | ✓ | ✓ | ✓ | ✓ |
| FP32 accumulation | always | always | always | always | always |

Implications for serving:

- **Turing (T4)**: FP16 + INT8 / INT4 quantization only.
- **Ampere (A100, A10)**: BF16-grade serving available; FP8 is not. Use weight-only INT4 (AWQ / GPTQ / Marlin) for quantization wins.
- **Ada (L40S, L4)**: FP8 serving available despite no HBM — a strong fit for INT4 / FP8 quantized LLMs.
- **Hopper (H100 / H200)**: full FP8 + TMA + WGMMA. FlashAttention-3, FlashInfer FP8 wrappers, DeepGEMM, etc. all target this tier first.
- **Blackwell (B200, RTX PRO 6000)**: adds FP4 — NVFP4 native and MXFP4 portable — plus block-scaled MMA that consumes scales without a pre-scale pass.

## Architecture-specific features

### Hopper (sm_90a)

| Feature | Purpose |
|:--------|:--------|
| **TMA** — Tensor Memory Accelerator | async bulk memory copy with swizzled indexing |
| **WGMMA** — warp-group MMA | async tensor-core matmul across 128 threads |
| **mbarrier** | on-chip async barrier |
| **Thread block clusters** | 2–16 CTAs cooperating via DSMEM |
| **DSMEM** — distributed shared memory | peer CTAs can read each other's shared memory |

Kernels that use these features compile for `sm_90a`, not `sm_90`.

### Blackwell (sm_100a)

| Feature | Purpose |
|:--------|:--------|
| **tcgen05** — Tensor Core Gen 5 | the new matmul instructions |
| **CTA-pair MMA** | two CTAs cooperate on one MMA (larger tiles) |
| **Block-scaled MMA** | consumes scale factors natively — enables MXFP* / NVFP4 without pre-scale |
| **Enhanced TMA** | larger async copies than Hopper TMA |
| **UMA / unified virtual space** (GB200 superchip only) | Grace CPU memory mappable from GPU |

### Ada / Ampere / Turing

No asynchronous-copy / warp-group / block-scaled MMA features. Standard synchronous tensor cores with per-generation precision support (see matrix above). Ada's FP8 path uses the same E4M3 / E5M2 formats as Hopper.

## Topology and NVLink domains

### NVLink presence by SKU

| SKU family | NVLink |
|:-----------|:-------|
| B200 HGX | NVLink 5, 1.8 TB/s bidir |
| RTX PRO 6000 Server | NVLink 5 |
| RTX PRO 6000 Workstation | PCIe only |
| H100 / H200 SXM5 | NVLink 4, 900 GB/s bidir |
| H100 PCIe | NVLink 4 (reduced) |
| L40S / L4 | PCIe only (no NVLink) |
| A100 SXM4 (40 GB / 80 GB) | NVLink 3, 600 GB/s bidir |
| A100 PCIe (40 GB / 80 GB) | NVLink 3 (reduced) |
| A10 | PCIe only |
| T4 | PCIe only |

### Within-node domains

- **HGX-8 Blackwell or Hopper**: 8 GPUs + NVSwitch = one NVLink domain. TP ≤ 8, EP ≤ 8 within-domain is cheap.
- **HGX-8 Ampere**: same 8-GPU NVLink-3 domain; narrower per-link bandwidth than Hopper.
- **GB200 NVL72**: 72 Blackwell GPUs + 36 Grace CPUs + NVSwitch 5 = a single NVLink-coherent domain spanning multiple trays. TP / EP within-domain ceilings rise from 8 to 72. First NVIDIA platform where NVLink crosses trays.

### Cross-node

Ethernet / InfiniBand with RDMA (RoCE / IB). NVLink does not span nodes on Hopper or earlier. On Blackwell, NVL72 forms one domain; anything past 72 GPUs still crosses NICs.

### PCIe-only SKUs

L40S, L4, A10, T4, and the RTX PRO 6000 Workstation have **no NVLink**. Multi-GPU serving on these cards uses PCIe / RDMA with correspondingly higher collective latency — prefer data-parallel replicas over TP when possible.

## See also

- [`hardware/amd-mi300/`](amd-mi300.md) — AMD Instinct MI300 family
- [`hardware/apple-silicon/`](apple-silicon.md) — Apple M-series
