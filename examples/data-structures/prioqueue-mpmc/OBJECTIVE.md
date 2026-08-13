Optimize a multi-producer, multi-consumer bounded priority queue.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./priority-queue-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/priority-queue/CANDIDATE_CONTRACT.md`.
- Implement prioritized enqueue and dequeue for copied byte values using the
  capacity and value size supplied by the trusted runner.

The candidate may use any language or combination of languages. Successful
enqueues may reserve capacity before publishing their `(priority, value)` item.
`FULL` observes reserved plus published items, while dequeue and `EMPTY` observe
only published items. Dequeue returns any published item at the lowest numeric
priority; equal-priority items may be returned in any order. The queue must not
fabricate or duplicate items. Maximize trusted end-to-end operation throughput
for the MPMC workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
