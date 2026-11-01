# Continuous batching on Trainium

Implements [`algorithms/continuous-batching.md`](../../algorithms/continuous-batching.md). Read the contract first — the five invariants hold here unchanged. What differs is the KV strategy, and it differs *by inversion*: on CUDA the goal is to eliminate padding; here padding to a bucket is the correct design.

## Why the CUDA design doesn't transfer

`torch.compile(backend="neuron")` compiles a graph for specific shapes (no dynamic shapes). A scheduler that varies batch size or sequence extent per step presents a new shape each time, and each new shape is a compile — seconds to minutes, on the serving hot path.

Variable-length packing and paged KV, the two CUDA answers, both exist specifically to make shapes dynamic. Porting either here maximizes the thing that hurts most.

## The design: static buffers, bucketed shapes

```
fixed batch slots B  ×  bucketed sequence extent L
      ↓
one compiled graph per (B, L) bucket pair
      ↓
admission fills slots; padding fills the remainder
```

- **Fixed slot count.** Pre-allocate `B` request slots. The batch tensor is always `B` wide; inactive slots are masked, not removed. Contract invariant 4 (independent finish) is satisfied by masking a slot, not by resizing the batch.
- **Bucketed extent.** Round each request's KV extent up to the next bucket. Recompile happens once per bucket, at warmup, not per request.
- **Padding is permanent.** It is not a stage to grow out of. The waste is real and is the price of static shapes.

## Bucket ladder

The ladder is the main tuning knob:

| Ladder | Effect |
|:--|:--|
| Few, wide buckets | Less compilation, more padded waste per request |
| Many, narrow buckets | Tighter fit, more compiled artifacts and longer warmup |

Start with powers of two over the expected prompt distribution (e.g. 512 / 1024 / 2048 / 4096) and one or two decode-extent buckets. Prefill buckets that are multiples of 2048 get SDPA's built-in NKI flash path ([`attention.md`](attention.md)). Every bucket must be compiled and cached at warmup: a bucket first reached in production compiles on the hot path and stalls every active request. Pick the smallest bucket that fits in host code, outside the compiled region, and reject or split inputs the ladder does not cover.

Combine with a persistent compile cache ([`floor.md`](floor.md) §2) so the ladder is paid for once across process restarts, not once per start.

## KV cache

Use the device-resident, in-place cache — [`static-kv-cache.md`](static-kv-cache.md). This is the decisive decode optimization and it composes directly with the static-slot design: the cache is allocated **once** at `(layers, slots, num_kv_heads, max_len, head_dim)` with `max_len` fixed at construction — not one cache per bucket. Buckets vary the compiled graph's extent over that single buffer, so admission and eviction are slot bookkeeping rather than allocation.

For plain continuous batching, do **not** hand-roll a block pool and page table: the buffers are statically sized, so there is no fragmentation to solve and the indirection costs without paying. **Prefix caching** is the exception; see the prefix section of [`static-kv-cache.md`](static-kv-cache.md).

## Satisfying the contract's invariants here

| Invariant | How |
|:--|:--|
| 1. Per-request position | Position tensor is `B`-wide; each slot carries its own extent |
| 2. Per-request masking | Mask covers both padding-to-bucket and inactive slots |
| 3. Admission between steps | Fill free slots before the step; never resize mid-step |
| 4. Independent finish | Mark the slot free and mask it; batch width is unchanged |
| 5. No per-request host sync | On-device sampling — see [`floor.md`](floor.md) §5 |

## Pitfalls

- **A bucket not in the ladder.** A compile stall mid-serving. Cover the workload's distribution at warmup.
- **Slot count as a tuning dial at runtime.** Changing `B` is a new shape. Fix it at compile time.
- **Host-side sampling.** A per-token device→host round trip dominates step time here; it undoes the batching win.
- **Assuming padded slots are free.** They consume the same compute as real ones. Utilization, not just throughput, should drive the slot count.
- **Copying a CUDA scheduler wholesale.** The tell is a KV allocator with a free list — that structure has no purpose on this backend.

## See also

- [`floor.md`](floor.md) — static shapes, compile cache, resident KV
- [`static-kv-cache.md`](static-kv-cache.md) — the resident in-place buffer model
- [`neuron-pytorch.md`](neuron-pytorch.md) — compile rules and host-sync pitfalls
