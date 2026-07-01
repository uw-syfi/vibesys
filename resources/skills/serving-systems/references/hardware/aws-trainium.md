# AWS Trainium for serving

Hardware spec reference for **Trainium2 (Trn2)** — the only Trainium generation
this note covers. For how PyTorch maps onto this hardware see
[`frameworks/neuron-pytorch.md`](../frameworks/neuron-pytorch.md); for writing
custom kernels, the vendored **`neuron-nki-*`** skills.

> Ignore Inf1/Trn1 and NeuronCore-v1/v2 material in older AWS docs — it does not
> apply here.

## Trainium2 chip at a glance

| Property | Trainium2 |
|:---------|:----------|
| Cores per chip | **8× NeuronCore-v3** |
| Device memory | **96 GiB HBM**, ~2.9 TB/s |
| Tensor engine (per core) | 158 FP8 / 79 BF16·FP16·TF32 / 20 FP32 dense TFLOPS |
| Tensor engine (per chip) | 1,299 FP8 / 667 BF16 dense TFLOPS; 2,563 sparse |
| Precisions | **FP8** (E4M3/E5M2), BF16, FP16, TF32, FP32 |
| DMA | inline HBM compression/decompression |

## NeuronCore-v3 — what a kernel author sees

Each NeuronCore-v3 is a separate accelerator with its own engines and on-chip
memory. The three-tier memory hierarchy (largest → smallest):

- **HBM** — 96 GiB device memory, shared by all cores on the chip.
- **SBUF** (scratchpad) — **28 MiB**, organized as **128 partitions × 224 KiB**.
  The "partition" axis is special: it's the parallel dimension the engines
  operate across. Working tiles live here.
- **PSUM** — **2 MiB** accumulator memory; matmul results land here before
  being copied back to SBUF.

Compute engines (work is dispatched to the right one):

- **Tensor (PE) engine** — matmuls / the systolic array. The throughput numbers
  above are this engine.
- **Vector engine** — elementwise / reductions across the free axis.
- **Scalar engine** — activations, scalar ops, transcendentals.
- **GpSimd** — general-purpose SIMD for irregular work.

Data flow is explicit: DMA **HBM → SBUF**, compute on engines (matmul →
**PSUM**, then back to SBUF), DMA **SBUF → HBM**. Hiding DMA behind compute is
the central performance problem. The NKI skills cover this in depth; the
[Trainium2 NKI architecture guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/guides/architecture/trainium2_arch.html)
is the canonical reference.

## Logical NeuronCores (LNC) — important for core counts

Trn2 supports a **logical NeuronCore** config that fuses physical v3 cores into
one logical core. With **LNC=2** (the Trn2 default), two physical cores present
as one logical core, so an 8-core chip shows up as **4 logical NeuronCores**.

This is why `neuron-ls` on a `trn2.3xlarge` reports **1 device / 4 cores /
96 GiB**: it's one full Trainium2 chip, LNC2. "4 cores" = 4 logical cores
(= 8 physical v3 cores). Treat the 4 logical cores as your unit of parallelism
(`NEURON_RT_VISIBLE_CORES`, tensor-parallel degree, etc.).

## Instance sizes

| Instance | Trn2 chips | Logical cores (LNC2) | Device mem |
|:---------|:-----------|:---------------------|:-----------|
| `trn2.3xlarge` | 1 (slice) | 4 | 96 GiB |
| `trn2.48xlarge` | 16 | 64 | 1.5 TiB |
| `trn2u.48xlarge` (UltraServer building block) | 16 | 64 | 1.5 TiB |

A single `trn2.3xlarge` (one chip, 4 logical cores, 96 GiB) comfortably holds an
8B model in BF16 (~16 GiB weights) with room for KV cache.

## Serving implications

- **Compiled, static-shape graphs.** NeuronCores run graphs compiled ahead of
  time by `neuronx-cc`; dynamic shapes trigger recompiles (minutes each). Bucket
  prompt/decode lengths. See [`frameworks/neuron-pytorch.md`](../frameworks/neuron-pytorch.md).
- **BF16 baseline, FP8 for throughput.** The tensor engine does ~2× FP8 vs BF16.
- **KV cache lives in HBM**; 96 GiB is generous for a single 8B model — favor
  large batch / long context before sharding.
- **Multi-core** via tensor/sequence parallelism across the logical cores is an
  optimization, not a requirement for an 8B model on one chip.

Sources: [Trainium2 Architecture](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trainium2.html),
[NeuronCore-v3](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/neuron-core-v3.html).
