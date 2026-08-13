Optimize a single-producer, multi-consumer bounded priority queue.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./priority-queue-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/priority-queue/CANDIDATE_CONTRACT.md`.
- Implement prioritized enqueue and dequeue for copied byte values using the
  capacity and value size supplied by the trusted runner.

The candidate may use any language or combination of languages. The queue must
remain linearizable, return lower numeric priorities first, never fabricate or
duplicate items, and respect capacity. Items with equal priority may be returned
in any order. Maximize trusted end-to-end operation throughput for the SPMC
workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
