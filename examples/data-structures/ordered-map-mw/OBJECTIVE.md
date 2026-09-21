Optimize a multi-writer concurrent ordered map.

Headline metric: `total_ops_per_sec` (maximize).

Preserve the required interface:
- Provide a native shared library named `./ordered-map-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/ordered-map/CANDIDATE_CONTRACT.md`.
- Implement put, get, remove, min, max, predecessor, successor, and range for
  copied byte keys and values. Ordered operations are required.

The correctness gate requires concurrent linearizability of point operations
(put, get, remove). Ordered operations (min, max, predecessor, successor,
range) that do not overlap a mutation match the sequential sorted map,
including range as a snapshot of `[start, end)` plus the truncation remaining
flag. Ordered operations that overlap a put or remove are weakly consistent
skip-list iteration, not snapshots. Keys compare as unsigned lexicographic
byte order. The map is unbounded. Maximize trusted end-to-end operation
throughput for the mixed-writer workload.

Start from the editable Rust implementation in `src/lib.rs`. It is an
intentionally naive correctness baseline, not part of the trusted evaluator.
