# Continuous batching — contract

New requests join a running generation loop without waiting for in-flight requests to finish. Every serving backend needs this; the *mechanism* differs enough per platform that the implementation lives under `platforms/`.

This file is the portable part: what any implementation must guarantee, and how to tell when it is wrong.

## The problem

A single-request loop serializes: request B waits for request A to emit its final token. At any realistic arrival rate the accelerator idles between requests while the scheduler waits on one sequence. Continuous batching decouples admission from completion — the batch composition changes between decode steps.

## Invariants

Any correct implementation, on any backend, must hold all five:

1. **Per-request position.** Each sequence's next token is at its *own* KV length, not the batch's. RoPE encodes absolute position; sharing one `past_len` across a mixed batch silently corrupts every request but the longest.
2. **Per-request masking.** Requests in a batch have different valid KV extents. Positions outside a request's own extent must contribute exactly zero to its attention output.
3. **Admission between steps, never mid-step.** The active set is read once per forward. Mutating it during a step races the scheduler against the accelerator.
4. **Independent finish.** A request that hits EOS or `max_tokens` leaves the batch without disturbing the others' KV state or positions.
5. **No per-request host sync on the hot path.** One synchronization per step, not one per request. This is where naive implementations lose most of their throughput.
6. **Admission is gated on KV capacity, and exhaustion has a defined policy.** Never admit a request whose KV cannot be allocated, and decide up front what happens when the cache fills mid-decode — preempt-and-recompute, swap out, or refuse. An implementation with no policy here degrades into thrashing or an OOM under exactly the load it was built for.

## Failure modes if skipped

| Symptom | Usually means |
|:--|:--|
| Throughput flat as concurrency rises | requests are serialized; admission isn't between-step |
| Output degrades only for short requests in a mixed batch | invariant 1 or 2 — position/mask derived from the batch, not the request |
| Per-step time scales with batch size, not with the largest member | per-request work on the hot path — host sync (invariant 5) is the usual culprit, but any per-request Python in the step does it |
| Throughput collapses or OOMs at high concurrency | no admission gating or no exhaustion policy (invariant 6) |
| Correct at batch 1, wrong at batch > 1 | masking of unused KV extent (invariant 2) |

## Platform implementations

The KV-storage strategy is the part that diverges, because it follows the backend's memory and shape model:

| Backend | Strategy | Why |
|:--|:--|:--|
| `cuda` | Remove padding from *attention* (variable-length packing or paged KV); bucket the *batch* dimension for graph capture | Padding wastes HBM and FLOPs; a capture miss costs an eager fallback, in microseconds |
| `rocm` | As cuda | Same shape model |
| `trainium` | Bucket **both** batch and sequence extent; pad up to the bucket | `torch.compile(backend="neuron")` has no dynamic shapes and compiles per shape, so a bucket miss costs a compile — seconds to minutes, not microseconds |
| `metal` | Unified memory, no separate device pool | Nothing to page; pressure is system-wide, not device-local |

Both accelerator families bucket and pad — what differs is **granularity** (batch only vs. batch *and* sequence extent) and the **cost of a miss** (eager fallback vs. a compile). That difference is large enough to change the design: on `cuda` you push padding out of attention and accept a shape ladder for capture; on `trainium` padding to a bucket is the answer at every level, and the CUDA advice to eliminate it will fight the compiler.

**Read your backend's file before implementing.**

## See also

- [`algorithms/paged-attention.md`](paged-attention.md) — KV storage substrate (where the backend has a discrete pool)
- [`algorithms/chunked-prefill.md`](chunked-prefill.md) — interleave long-prompt prefill with active decodes
- [`algorithms/batched-sampling.md`](batched-sampling.md) — how invariant 5 is satisfied on the sampling side
- [`platforms/`](../platforms/) — the implementation for the selected backend
