# The MLX serving loop

Serving-specific patterns on top of [`mlx.md`](mlx.md) (the framework) and [`continuous-batching.md`](continuous-batching.md) (the scheduler). Covers KV cache layout, streaming, and the sampling path under unified memory and lazy evaluation.

## KV cache

Keep KV as plain per-request `mx.array` pairs per layer. Two viable growth strategies:

| Strategy | Shape | Trade |
|:--|:--|:--|
| **Append-and-reallocate** | grows exactly | simple; reallocates every step |
| **Preallocate to a cap, track extent** | fixed `(B, H, L_max, D)` | one allocation; wastes up to `L_max` per slot |

Preallocate-and-track is usually better despite the waste — it avoids per-step allocator churn inside the lazy graph, which shows up as graph-construction overhead rather than as an obvious hotspot.

Do not build a block pool. See [`continuous-batching.md`](continuous-batching.md) for why paging has no purpose here.

## Attention

Use `mx.fast.scaled_dot_product_attention`. It fuses the softmax and avoids materializing the `(L, L)` attention matrix — the same activation-peak argument as FlashAttention, different API.

Pass the causal mask explicitly for prefill; for single-token decode the mask covers each slot's valid extent (contract invariant 2).

## Sampling

Keep the whole sampling pipeline in MLX arrays:

```
logits ─► penalties ─► temperature ─► top-k / top-p ─► categorical sample
```

Every stage stays lazy; nothing materializes until the step's single `mx.eval`. Sampling parameters are per-request arrays of length `B`, exactly as on other backends — the vectorization argument in [`algorithms/batched-sampling.md`](../../algorithms/batched-sampling.md) is fully portable.

The one Metal-specific trap: comparing a sampled token against an EOS id in Python forces materialization. Do the comparison in MLX and fold it into the same `mx.eval`.

## Streaming

Detokenization needs the token on the host, so streaming forces one materialization per step — which is fine, because the loop already has exactly one.

Order matters: `mx.eval` the step, then detokenize and push, then build the next step. Pushing before evaluating either blocks or reads an unmaterialized array.

UTF-8 safety is unchanged from any other backend — buffer partial multi-byte sequences across steps rather than decoding each token independently. See [`tooling/io-handling.md`](../../tooling/io-handling.md).

## Quantized weights

`mx.quantize` produces group-wise INT4/INT8 that the fast kernels consume directly — no separate dequantize step in the graph. On a bandwidth-bound backend this is the highest-leverage change available; see [`floor.md`](floor.md) §5.

Quantized and unquantized layers can coexist. The usual split is quantized linear layers with the embedding and final projection left at higher precision.

## Warmup

`mx.compile` compiles on first call with a given shape, so the first request of each shape pays for it. Run a synthetic prefill and a few decode steps at the shapes you intend to serve before accepting traffic — otherwise the first real request absorbs compilation and skews every TTFT measurement.

## Pitfalls

- **Materializing inside the layer loop.** One `mx.eval` per step, at the end. Not per layer.
- **Python-side EOS comparison.** Forces a sync; fold into the step's evaluation.
- **Unbounded KV growth.** System memory is shared; a runaway cache causes swapping, not a clean OOM.
- **Benchmarking without warmup.** First-call compilation lands in TTFT.
- **Assuming `mx.compile` is CUDA graphs.** It fuses ops rather than capturing launches, so the stable-address rule drops away — but it still recompiles per traced shape, so the shape discipline does not.

## See also

- [`mlx.md`](mlx.md) — array model, lazy eval, `mx.fast`, custom Metal kernels
- [`continuous-batching.md`](continuous-batching.md) — the scheduler this sits inside
- [`hardware.md`](hardware.md) — bandwidth per SKU, for the roofline
