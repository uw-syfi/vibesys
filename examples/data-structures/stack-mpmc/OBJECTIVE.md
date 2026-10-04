Optimize a multi-producer, multi-consumer bounded stack.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./stack-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/stack/CANDIDATE_CONTRACT.md`.
- Implement try-style push and pop for copied byte values using the capacity
  and value size supplied by the trusted runner.

The candidate may use any language or combination of languages. Successful
pushes may reserve capacity before publishing their value. `FULL` observes
reserved plus published items, while pop and `EMPTY` observe only published
items. Pop returns the last published item. The stack must not fabricate or
duplicate items. Maximize trusted end-to-end operation throughput for the MPMC
workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
