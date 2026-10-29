# Trainium (Neuron) optimization floor

Trainium is not a GPU. The optimization floor is genuinely different, and several CUDA reflexes are actively counterproductive here. Start from this file, not from the CUDA one. The stack is native TorchNeuron: the `neuron` PyTorch device, eager mode, and `torch.compile(backend="neuron")` — see [`neuron-pytorch.md`](neuron-pytorch.md).

## 1. Compiled, static-shape hot path

Eager mode dispatches one compiled op at a time; the decode loop must run as `torch.compile(backend="neuron", fullgraph=True)` regions. Each graph compiles for fixed shapes, and a new shape is a new compile (seconds to minutes), so a dynamically-shaped serving loop that would be optimal on CUDA is pathological here.

- **Bucket** prompt and decode extents and the batch width; pad up to the nearest bucket.
- **Padding is correct on this backend**, not a baseline to eliminate. The CUDA guidance to remove padding via variable-length packing does not transfer.
- Keep the bucket ladder small — each bucket is a separate compiled artifact — and compile every bucket at startup.
- Zero graph breaks and zero recompiles after warmup: check with `TORCH_LOGS="graph_breaks,recompiles"` and `get_dynamo_metrics()`.

## 2. Persistent compile cache

Without a warm cache, every process start pays full compilation. VibeSys's trainium backend points the NEFF, HLO and NKI caches at a host directory that persists across rounds. A "slow startup" report on Trainium is almost always a cold cache or a shape outside the ladder.

## 3. Device-resident, in-place KV cache

**The decisive decode optimization.** Preallocate the cache on `neuron` once and update it in place from inside the compiled decode step; only token ids and positions go in and sampled ids come out.

- [`static-kv-cache.md`](static-kv-cache.md)

A decode that keeps the cache on CPU, or returns the whole cache from each step and rebinds it, moves the cache across the device boundary every token. It presents as host-bound, not as a slow kernel. This is the single most common Trainium serving mistake.

The default is *not* paged attention: with static shapes there is no fragmentation problem to solve, so plain continuous batching over fixed slots needs no block pool.

## 4. Flash attention via SDPA / NKI

Flash attention is **not** NVIDIA-only. `F.scaled_dot_product_attention` in a compiled region uses a built-in NKI flash kernel when the query length is a multiple of 2048, and `nkilib` ships prefill and decode attention kernels.

- [`attention.md`](attention.md)

Use them instead of a naive `softmax(QKᵀ)V` that materializes FP32 scores — that activation peak caps batch size on a 24 GiB core.

## 5. BF16 and on-device sampling

BF16 is the serving precision; FP8 matmuls need Trainium3, so dequantize FP8 checkpoints at load on trn2. Keep sampling on device and read back only the sampled ids — a full-logits host copy per token dominates step time at Trainium's decode latency.

## Then

- **Continuous batching over static slots** — contract at [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md), implementation at [`continuous-batching.md`](continuous-batching.md).
- **Tensor parallelism** across logical cores when weights + cache exceed one core — [`tensor-parallel.md`](tensor-parallel.md).
- **Custom NeuronCore kernels** — [`nki-kernels.md`](nki-kernels.md) for the PyTorch side and the ready-made `nkilib` kernels, and the bundled `neuron-nki-*` skills (`neuron-nki-writing`, `-docs`, `-debugging`, `-profiling`, `-profile-querying`) for writing them. These are the Trainium analog of writing Triton/CUTLASS kernels, and unlike NVIDIA kernel authoring they are *in scope* for this collection.

## What does not apply

| CUDA technique | On Trainium |
|:--|:--|
| CUDA graphs / `mode="reduce-overhead"` | N/A — `torch.compile(backend="neuron")` already produces one compiled executable per graph; only `mode="default"` exists |
| `dynamic=True` / `mark_dynamic` | N/A — no dynamic shapes; bucket instead |
| Paged attention / block pool | N/A for plain batching — see [`static-kv-cache.md`](static-kv-cache.md) |
| Eliminating padding | **Inverted** — bucketed padding is required |
| FlashInfer / FlashAttention libraries | N/A — SDPA's NKI flash path or `nkilib` attention kernels |
| `torch_xla`, `torch_neuronx.trace`, NxD Inference | Retired stack, not in the container |

## See also

- [`hardware.md`](hardware.md) — NeuronCore-v3, SBUF/PSUM/HBM hierarchy, LNC config
- [`neuron-pytorch.md`](neuron-pytorch.md) — TorchNeuron, compile rules, caches, host-sync pitfalls
- [`profiler.md`](profiler.md) — neuron-explorer
