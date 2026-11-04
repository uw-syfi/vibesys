# Overlap scheduling on Apple Silicon

Implements [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md). The contract's problem statement applies — host scheduler work can stall the device — but the mechanism is the opposite of the CUDA one, and porting the CUDA design here makes things worse.

## Why the CUDA design is wrong here

The CUDA implementation builds an explicit two-stream pipeline with events ordering a non-blocking D2H copy against the next step's preparation.

MLX *does* have streams — `mx.new_stream`, `mx.default_stream`, the `mx.stream()` context manager, `mx.synchronize()`, and a `stream=` kwarg on every op. So the reason not to port the CUDA design is not "the API is missing."

The reason is that MLX is **already deferred by default** and **inserts cross-stream dependencies automatically**. Operations build a graph; nothing executes until something forces materialization, and the runtime orders work across streams for you. The overlap the CUDA design constructs by hand with explicit `wait_stream` ordering is what lazy evaluation plus automatic dependency tracking already provides.

Hand-rolling a two-stream pipeline therefore adds bookkeeping without adding overlap. Reach for `mx.stream()` when you genuinely want independent work on separate queues (e.g. overlapping an unrelated encoder), not to rebuild the decode pipeline.

## The actual lever: where you evaluate

Overlap is controlled entirely by `mx.eval` / `mx.async_eval` placement.

```
# serialized — host waits for the device every step
tokens = sample(model(step_input))
mx.eval(tokens)                      # blocks here
next_input = prepare(tokens)         # host work starts only after

# overlapped — host prepares while the device computes
tokens = sample(model(step_input))
mx.async_eval(tokens)                # kick off, don't block
... host-side bookkeeping for the next step ...
mx.eval(tokens)                      # settle just before the value is needed
```

`mx.async_eval` starts execution without blocking. Host work between the two calls runs concurrently with device compute. That is the whole technique.

## Satisfying the contract's invariants here

| Invariant | How |
|:--|:--|
| 1. Pipeline depth 2 | Natural — one step in flight while the host prepares the next. Deeper needs explicit buffering and is rarely worth it. |
| 2. Future-typed values | Automatic — an unevaluated `mx.array` *is* the future. Reading it in Python is what forces materialization. |
| 3. One ordered sync point | The `mx.eval` that settles the step. Everything the host reads funnels through it. |
| 4. Serialize on demand | Grammar-constrained sampling and anything needing a host-side decision force materialization inherently; the loop degrades to synchronous with no special handling. |

Invariant 2 is worth noting: on CUDA it takes deliberate machinery (`FutureMap`, index-into-preallocated-buffer). Here it is the default, and the *violation* is what takes effort — every accidental `.item()` is a broken future.

## Finding the stalls

A profile showing regular host blocks once per token means something is materializing inside the loop. Common sources:

- `.item()` / `float()` / `int()` on an MLX array
- `print` or logging of a token value
- Python-side `if token == eos_id`
- `mx.eval` called per layer or per tensor instead of once per step

See [`profiler.md`](profiler.md) for the trace pattern.

## What does not apply

| CUDA concern | Here |
|:--|:--|
| Explicit `wait_stream` ordering | Unnecessary — MLX inserts cross-stream dependencies automatically (`mx.stream()` exists if you want separate queues) |
| CUDA events as memory barriers | N/A |
| Non-blocking D2H copy | N/A — no transfer |
| Graph capture interacting with multi-stream regions | N/A |

## See also

- [`floor.md`](floor.md) §2 — lazy evaluation in the optimization floor
- [`mlx.md`](mlx.md) — evaluation semantics
- [`continuous-batching.md`](continuous-batching.md) — the loop this optimizes
