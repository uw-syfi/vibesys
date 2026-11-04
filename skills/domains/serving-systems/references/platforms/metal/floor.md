# Apple Silicon (Metal / MLX) optimization floor

Unified memory changes the problem. The dominant CUDA concerns — host↔device transfer, KV pool fragmentation, kernel launch overhead — either don't exist here or have a different shape. Serving on Apple Silicon is almost always **memory-bandwidth bound at batch 1**, and the floor reflects that.

## 1. No host↔device staging

CPU and GPU share physical memory. There is no `.to(device)` cost, no pinned-memory dance, no D2H copy to hide.

Consequences: patterns built to amortize transfer cost are pure overhead here, and a "GPU memory" budget is really a *system* memory budget shared with everything else running.

## 2. Lazy evaluation — control where it materializes

MLX builds a graph and defers execution. `mx.eval()` forces materialization; where you place it *is* the scheduling decision.

- Evaluate once per decode step, not per tensor.
- Use `mx.async_eval` to start a step's compute while the host prepares the next.
- A stray `.item()`, `float()`, or print inside the loop forces a synchronous materialization and serializes the pipeline. This is the equivalent of the CUDA per-request sync problem, and it is the most common MLX serving bug.

Do **not** build an explicit two-stream pipeline on top of this — see [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md); forcing materialization to orchestrate streams removes the overlap it was meant to create.

## 3. `mx.compile` for the step function

Fuses elementwise chains and removes graph-construction overhead per step. Apply to the decode step function, not to individual ops.

This is the nearest analog to CUDA graphs, but it is kernel fusion, not launch-capture. Address stability is a non-issue (MLX arrays are immutable), but **shape stability still matters**: `mx.compile` caches by traced shape and dtype and recompiles on a new shape. Warm it at the shapes you intend to serve.

## 4. Fused attention via `mx.fast`

Use `mx.fast.scaled_dot_product_attention` rather than materializing the attention matrix. The activation-peak argument is the same as everywhere else; the API is not FlashAttention.

For anything not covered, `mx.fast.metal_kernel` allows a custom Metal kernel without leaving Python.

## 5. Native quantization

`mx.quantize` provides group-wise INT4/INT8 with no external toolchain. On a bandwidth-bound backend this is usually the single largest win — it directly reduces the bytes moved per token.

Group size 64 is a reasonable default; 32 for quality-sensitive work.

## Then

- **Continuous batching** — worthwhile but differently shaped; contract at [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md), implementation at [`continuous-batching.md`](continuous-batching.md).
- **KV cache layout** — [`mlx-serving.md`](mlx-serving.md).

## What does not apply

| CUDA technique | On Metal |
|:--|:--|
| CUDA graphs | N/A — `mx.compile` fuses, it does not capture |
| Paged attention / block pool | N/A — no discrete device pool to fragment |
| KV tiering to CPU / NVMe | N/A — CPU memory *is* the same memory |
| Multi-device TP / PP / EP | N/A — single SoC |
| Disaggregated prefill/decode | N/A — no fabric to disaggregate across |
| FlashInfer / FlashAttention | N/A — use `mx.fast` |

## Sizing

Bandwidth, not FLOPs, sets decode speed. Check [`hardware.md`](hardware.md) for the per-SKU figure — the spread across M-series variants is several-fold, and it predicts tokens/sec more directly than any other number.

## See also

- [`mlx.md`](mlx.md) — the framework: array model, lazy eval, compile, fast kernels
- [`mlx-serving.md`](mlx-serving.md) — the serving loop under unified memory
- [`profiler.md`](profiler.md) — Instruments
