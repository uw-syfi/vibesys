# Speculative decoding on Trainium

Implements [`algorithms/speculative-decoding.md`](../../algorithms/speculative-decoding.md). The five invariants hold and the drafter variants are unchanged. The divergence is **variable accepted length**, which on a static-shape backend is a shape change per step — the thing that costs most here.

## The problem

Accepted length varies per step in `[0, k]`. On CUDA that means capturing a graph per bucket and padding up. Here each distinct accepted length that reaches the compiler is a *compile*, so a naive implementation triggers a recompile storm on the serving hot path and is slower than not speculating at all.

## The design: fixed-shape verify, masked accept

Do not let accepted length reach the graph shape.

- **Verify at fixed width `k+1`, always.** The verify forward's shape is constant regardless of how many tokens will be accepted. This satisfies contract invariant 1 (one target forward) at a shape the compiler sees once.
- **Compute the accept prefix on device.** Comparison and prefix-stop are elementwise over a `k+1`-wide tensor; the result is a length *value*, not a shape.
- **Advance the KV extent by a masked write, not a resize.** The resident in-place cache ([`static-kv-cache.md`](static-kv-cache.md)) is already sized to `max_len`; commit accepted positions by masking, and let rejected positions be overwritten on the next step.
- **Never branch the graph on accepted length.** A Python `if accept_len == n` that selects a differently-shaped path defeats the whole design.

Net: one compiled verify graph per `(batch bucket, k)`, not per accepted length.

## Drafter

The drafter needs its own compiled graphs and its own bucket ladder, and contract invariant 2 (incremental decode) is *more* important here than on CUDA — a drafter that re-runs a full forward over a growing context presents a new shape every token, so it compiles every token.

If the drafter shares the target's tokenizer, both read the same token id stream, but each still needs its own resident KV and bucket ladder.

## Whether to speculate at all

The gating discipline in the contract applies, with an added prerequisite: **the compile-side cost must be amortized before measuring.** Warm every `(bucket, k)` graph for both drafter and target, then measure rolling-average effective tok/s over N≥5 warm requests.

Evaluate the floor items first. Speculative decoding is a later-stage optimization; if the hot path is still eager or the KV cache is still crossing the device boundary per token ([`floor.md`](floor.md) §1, §3), fixing that yields more and makes the spec-decode measurement meaningful.

## Satisfying the contract's invariants here

| Invariant | How |
|:--|:--|
| 1. One target forward | Fixed `k+1` width verify |
| 2. Drafter decodes incrementally | Own resident KV + own bucket ladder |
| 3. Accept prefix then stop | On-device prefix-stop; produces a value, not a shape |
| 4. Roll back all length trackers | Masked commit into the resident buffer; extent tracker (a device tensor) moves with it |
| 5. Verify uses the normal execution path | Same compiled region as ordinary decode, at width `k+1` |

## Pitfalls

- **Accepted length reaching the shape.** The characteristic failure. Symptom: throughput collapses and compile activity appears mid-run in the profile.
- **Branching to a differently-shaped verify path.** Same cause, different spelling.
- **Unbucketed drafter.** Compiles per token.
- **Measuring before warmup.** Every graph must be warm or the measurement reads compilation.
- **Assuming CUDA capture guidance transfers.** There is no capture step; the requirement is compile-time bucketing.

## See also

- [`floor.md`](floor.md) — higher-priority optimizations to land first
- [`continuous-batching.md`](continuous-batching.md) — the bucket ladder this extends
- [`static-kv-cache.md`](static-kv-cache.md) — the resident buffer accepted tokens commit into
