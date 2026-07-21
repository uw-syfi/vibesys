Optimize a multi-producer, multi-consumer bounded FIFO queue.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./queue-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/queue/CANDIDATE_CONTRACT.md`.
- Implement enqueue and dequeue for copied byte values using the capacity and
  value size supplied by the trusted runner.

The candidate may use any language or combination of languages. Successful
operations must preserve one global FIFO order, the queue must not fabricate or
duplicate items, and reservations plus published items must respect capacity.
An in-flight enqueue may reserve capacity before publication: `FULL` observes
reserved plus published capacity, while `EMPTY` observes published items only.
Maximize trusted end-to-end operation throughput for the MPMC workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
