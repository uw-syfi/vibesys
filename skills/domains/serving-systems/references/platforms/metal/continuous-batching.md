# Continuous batching on Apple Silicon

Implements [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md). The five invariants hold. What changes is that there is no separate device memory pool, so the entire CUDA apparatus for managing one — block pools, page tables, fragmentation avoidance — has nothing to manage.

## What unified memory removes

| CUDA concern | Here |
|:--|:--|
| KV pool sized against device VRAM | The pool is system RAM, shared with the OS and everything else |
| Fragmentation of a dedicated pool | Handled by the system allocator |
| Page table indirection | No purpose — allocate normally |
| Eviction to CPU RAM | Meaningless; it is already the same memory |

Practically: keep a per-request KV array and let MLX's allocator handle it. Implementing paged attention here adds indirection and buys nothing.

## What the batch is actually for

On an accelerator, batching amortizes launch overhead and fills parallel units. Here the dominant win is amortizing **weight reads** — decode at batch 1 is bandwidth-bound, streaming the full weight set per token. Serving two sequences from one pass roughly halves bytes-per-token.

That reframes the tuning: the useful batch size is set by how much bandwidth pressure it relieves before compute becomes the bound, not by how many launches it hides. On a bandwidth-bound backend the curve flattens sooner than CUDA intuition expects.

## The loop

```
per step:
    admit new requests (between steps — invariant 3)
    build B-wide token / position / mask arrays
    logits = model(step_input)          # lazily built graph
    tokens = sample(logits)             # stays an mx.array
    mx.eval(tokens, kv_state)           # ONE materialization per step
    deliver, retire finished slots
```

**The single most important line is `mx.eval`.** One per step, covering everything the next step needs. Contract invariant 5 — no per-request host sync — becomes "no per-request `.item()`", and the failure is identical: throughput that does not improve with concurrency.

Use `mx.async_eval` to begin a step's compute while the host prepares the next; see [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md). Do not build a two-stream pipeline — forcing materialization to orchestrate it destroys the overlap.

## Memory pressure is a system property

The KV pool competes with the OS, the page cache, and every other application. Two consequences:

- Sizing the cache to "all available memory" is more dangerous than on a discrete accelerator — the failure mode is system-wide swapping, not a clean device OOM.
- Leave real headroom. A machine that starts paging degrades far past the point a GPU OOM would have failed cleanly.

## Satisfying the contract's invariants here

| Invariant | How |
|:--|:--|
| 1. Per-request position | Position array is `B`-wide, per-slot extent |
| 2. Per-request masking | Additive mask over each slot's valid extent |
| 3. Admission between steps | Admit before building the step's arrays |
| 4. Independent finish | Drop the slot and rebuild arrays next step |
| 5. No per-request host sync | One `mx.eval` per step; no `.item()` in the loop |

## Pitfalls

- **`.item()` / `float()` / `print` inside the loop.** Forces synchronous materialization; serializes everything. The most common MLX serving bug.
- **Porting a paged KV allocator.** Pure overhead here.
- **Sizing KV against "GPU memory".** There is no separate budget.
- **Expecting CUDA batch-scaling.** The bandwidth bound flattens the curve earlier.
- **`mx.eval` per tensor rather than per step.** Each call is a synchronization point.

## See also

- [`floor.md`](floor.md) — the Apple Silicon optimization floor
- [`mlx-serving.md`](mlx-serving.md) — KV layout and streaming detail
- [`mlx.md`](mlx.md) — lazy evaluation semantics
