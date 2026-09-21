Optimize a concurrent unordered map for a mixed multi-writer workload.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./unordered-map-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/unordered-map/CANDIDATE_CONTRACT.md`.
- Implement put, get, and remove for copied byte keys and values using the
  maximum sizes and client count supplied by the trusted runner.

The candidate may use any language or combination of languages. The map has no
ordering, range, or iteration contract. The trusted checker requires
linearizable concurrent put, get, and remove. Maximize trusted end-to-end
operation throughput for the mixed multi-writer workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
