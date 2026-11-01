# Overlap scheduling on Trainium

Implements [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md). The contract's problem — host scheduler work stalling the device — applies directly: TorchNeuron enqueues work asynchronously, so the host can run ahead of the device, and every sync point it hits drains the queue.

## The model

A call to a compiled region (or an eager op) on `neuron` tensors enqueues work and returns. The device runs the queue in order. The host only waits at sync points: `.cpu()`, `.item()`, `.tolist()`, `print(tensor)`, `torch_neuronx.synchronize()`.

A server must read the sampled token ids every step (to detokenize, stream, and decide which requests finished). Done naively that read is a full sync, and the device idles for the whole of the host's per-step work.

## The lever: copy step N's ids without waiting for step N+1

Feed step N's sampled ids (a device tensor) straight into step N+1, enqueue N+1, and only then wait for step N's ids — with a non-blocking copy that was ordered before N+1:

```python
prev = step(tok)                                   # step 0 enqueued
while serving:
    ids_host = prev.to("cpu", non_blocking=True)   # D2H of step N, queued before N+1
    copied = torch.neuron.Event(); copied.record()
    nxt = step(prev)                               # step N+1 enqueued from device-side ids
    copied.synchronize()                           # waits for step N + its copy only
    scheduler.on_tokens(ids_host.tolist())         # detokenize/stream/admit while N+1 runs
    prev = nxt
```

Measured on trn2 (SDK 2.33), a compiled step with 0.5 ms of host work per token: **1.54 ms/step** synchronous (`tok = step(tok); tok.cpu()`), **0.94 ms/step** with the pattern above. Reading `prev.cpu()` after enqueueing `nxt` without the event does **not** overlap (1.58 ms/step): a blocking copy on the default stream waits for everything queued before it, including step N+1. Issuing the copy on a side stream (`torch_neuronx.Stream`, `stream.wait_event`) gives the same result as the event pattern.

## Satisfying the contract's invariants here

| Invariant | How |
|:--|:--|
| 1. Pipeline depth 2 | Step N+1 is enqueued before the host consumes step N |
| 2. Future-typed values | Sampled ids stay a device tensor and feed the next step directly; the host copy is the only materialization |
| 3. One ordered sync point | `copied.synchronize()` once per step |
| 4. Serialize on demand | Admission into a new bucket, a stop condition that changes the next step's inputs, or grammar masks need step N's result before N+1: settle, then enqueue (the loop degrades to synchronous for that step) |

Finished requests are handled one step late: step N+1 already ran for a slot whose request ended at step N. Mask that slot's output and free it; this costs one wasted slot-step, not correctness.

## The dominant stall is upstream of this

On Trainium the usual cause of device idle is **not** scheduler overhead. In order of frequency:

1. **An eager hot path** — thousands of per-op dispatches per step. Compile it ([`floor.md`](floor.md) §1).
2. **KV crossing the device boundary per token** — see [`floor.md`](floor.md) §3. Presents as host-bound; fix this before anything here.
3. **Host-side sampling** — a full-logits copy per token. Sample on device.
4. **A recompile mid-run** — an unbucketed shape reached the compiler. Fix the bucket ladder in [`continuous-batching.md`](continuous-batching.md).
5. *Then* scheduler-level overlap.

Applying this file's technique before fixing 1–4 optimizes a small term while a large one dominates. The profiler ([`profiler.md`](profiler.md)) distinguishes them: eager dispatch, recompiles and boundary transfers are visible on the timeline; scheduler overhead is the residual gap once they are gone.

## What does not apply

| CUDA concern | Here |
|:--|:--|
| CUDA graph capture inside an event-ordered region | N/A — compiled regions are already one executable |
| `cudaStreamSynchronize` / `torch.cuda` events | Use `torch.neuron.Event`, `torch_neuronx.Stream`, `torch_neuronx.synchronize()` |

## See also

- [`floor.md`](floor.md) — the higher-priority optimizations
- [`static-kv-cache.md`](static-kv-cache.md) — why step N+1 needs nothing from the host
- [`neuron-pytorch.md`](neuron-pytorch.md) — sync points and timing
- [`profiler.md`](profiler.md) — telling these stalls apart
