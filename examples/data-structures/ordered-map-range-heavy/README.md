# Ordered Map Range-Heavy Input

This input targets a multi-writer ordered map whose timed mix is mostly
successor and range. The manifest invokes the trusted Go evaluator directly.
Candidates implement the copying C ABI documented at
`_evaluator/ordered-map/CANDIDATE_CONTRACT.md` and export it from
`./ordered-map-candidate.so`. Ordered neighbor and range operations are
required.

The shared seed at `examples/starters/ordered-map-rs` provides an editable
`src/lib.rs` with an intentionally naive Rust candidate using one mutex and
`BTreeMap`. Build and validate it from a materialized workspace:

    make
    go -C _evaluator/ordered-map run . check --workspace "$PWD" --scenario range-heavy
    go -C _evaluator/ordered-map run . benchmark --workspace "$PWD" --scenario range-heavy --duration 1s --warmup 0s

The starter is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The gate requires concurrent
linearizability of point operations. Ordered operations that overlap a
mutation are weakly consistent. The manifest benchmark uses three repetitions
and reports their median as `total_ops_per_sec`.
