# Async / overlap scheduling — contract

At low-to-medium batch size the host-side scheduler is slow enough that the accelerator idles between decode steps. Async scheduling hides that host work behind accelerator compute.

The problem is universal. The mechanism is not — it depends entirely on how the backend expresses asynchrony, so the implementation lives under `platforms/`.

## The problem

Per decode step the host must: pick the next batch, build KV/block metadata, assemble kernel inputs, detokenize the previous step, and push responses. At 1–5 ms per iteration this dominates TPOT at batch 1–8, where the accelerator forward is comparably short. The accelerator finishes and waits.

Two distinct stalls, often confused:

1. **Launch-level** — per-kernel dispatch cost. Addressed by graph capture / fusion where the backend has it; see the backend's own note.
2. **Scheduler-level** — the Python work above. This file's subject.

Fixing (1) does not fix (2), and a profile that shows gaps *between* steps rather than *between kernels* is pointing at (2).

## Invariants

1. **Pipeline depth 2.** While the accelerator runs step N, the host prepares N+1 and post-processes N−1. Deeper adds queueing latency and in-flight KV without hiding more.
2. **Future-typed values.** Step N+1's preparation needs "what will step N sample?" — a value not yet materialized. It must be referenced indirectly (index into a preallocated buffer, or a future), never read.
3. **One ordered sync point.** Every host read of sampled tokens funnels through a single completion signal. Reading earlier either blocks (defeating the pipeline) or reads garbage.
4. **Serialize on demand.** Some states — grammar-constrained sampling, speculative verify, pipeline parallelism — force a stage to block. The pipeline must degrade to synchronous rather than race.

## Failure modes if skipped

| Symptom | Usually means |
|:--|:--|
| Accelerator idle gaps *between* steps, not between kernels | scheduler-level stall — this file |
| Sporadic wrong tokens under load | invariant 3 — a host read that bypassed the sync point |
| Latency rises when the pipeline is enabled | depth > 2, or a per-step serialization point (invariant 4) |
| Gains vanish at large batch | expected — host overhead amortizes; this optimization targets small batch |

## Platform implementations

| Backend | Mechanism |
|:--|:--|
| `cuda` | Two streams + events; `wait_stream` ordering, non-blocking D2H, completion event before host read |
| `rocm` | As cuda (HIP streams/events) |
| `trainium` | TorchNeuron enqueues compiled regions asynchronously; overlap comes from feeding step N's device-side ids into step N+1 and copying step N's ids back non-blocking behind an event, so the host never waits on N+1 |
| `metal` | MLX defers by default and auto-orders across streams, so overlap is controlled by *where* `mx.eval` / `mx.async_eval` are placed rather than by explicit stream/event choreography |

`metal` is the case where a direct port of the CUDA design is actively wrong: constructing an explicit two-stream pipeline on top of a lazy graph forces materialization and removes the overlap it was meant to create.

## See also

- [`algorithms/continuous-batching.md`](continuous-batching.md) — the scheduling substrate this overlays
- [`algorithms/batched-sampling.md`](batched-sampling.md) — removes another sync source on the same critical path
- [`tooling/profiler.md`](../tooling/profiler.md) — how to tell stall (1) from stall (2)
- [`platforms/`](../platforms/) — the implementation for the selected backend
