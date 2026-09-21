Optimize a multi-producer, single-consumer bounded stack.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./stack-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/stack/CANDIDATE_CONTRACT.md`.
- Implement try-style push and pop for copied byte values using the capacity
  and value size supplied by the trusted runner.

The candidate may use any language or combination of languages. The stack must
remain linearizable, return values in LIFO order, never fabricate or duplicate
items, and respect capacity. Duplicate payloads occupy distinct slots; pop
order follows push linearization, not value equality. Maximize trusted
end-to-end operation throughput for the MPSC workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
