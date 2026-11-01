# Specializing a bespoke serving engine

Applies when the engine serves one model on one accelerator for one stated workload class, and
nothing else is required of it. Generality is then not a cost: every runtime branch, generic
stride, dynamic shape or allocation that the fixed setting makes unnecessary is overhead.

## Specialize only for a measured benefit

Specialization is a means. Keep a hardcode, prepack, tuned config or fused path only if it
shows a measured benefit (throughput, latency, memory footprint, startup time) that clears
noise, and say which in the commit message. A specialization with no benefit is added code
and added risk: leave it out, or take it back out. Prefer the simplest specialized form that
captures the benefit.

## Specialize to these three things

1. **The model.** Hardcode layer counts and types, head counts and dims, vocabulary size.
   Write one kernel per layer type and per `(N, K)`, with no shape arguments resolved at run time.
   Prepack weights once at load into the layout each kernel streams best (tile-contiguous,
   swizzled, gate and up interleaved). Precompute RoPE tables. Fix the memory plan: pools and
   workspaces allocated once, none in the hot path.
2. **The hardware.** Tile sizes, warp counts, split-K and persistent-grid sizes chosen per
   `(rows, N, K)` from measurements on this device, against its measured, not nominal, bandwidth
   and matrix rate. Separate kernel families per row-count bucket. Capture graphs per fixed
   bucket.
3. **The workload class.** Shape the scheduler and cache for what the class does, as stated in
   the task. For an agentic multi-turn coding workload that means, for example: long shared
   prefixes and a high prefix-cache hit rate, so protect cached blocks and make cache resume
   cheap; many short decodes after large prompt suffixes, so prefill chunking and the
   prefill-to-decode handoff matter; sessions whose context grows each turn; bounded
   concurrency, so size buckets and pools to it.

## The limit

Specialize to the class, never to the benchmark's own content. Recognizing particular prompts
or trace patterns, keying a cache or a schedule on benchmark identity, or tuning to a fixed
request list is gaming and fails review. When a choice depends on the workload's shape (row
range, context range, arrival pattern), say so in the commit message so a reviewer can check
that it follows from the stated class.

Keep correctness limits: the accuracy checker still applies, precision rules in the task still
apply, and a specialization that silently changes outputs is not a specialization.
