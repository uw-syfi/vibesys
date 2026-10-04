# Unordered Map SWMR Input

This input targets a single-writer, multi-reader concurrent unordered map. The
manifest invokes the trusted Go evaluator directly. Candidates implement the
copying C ABI documented at
`_evaluator/unordered-map/CANDIDATE_CONTRACT.md` and
export it from `./unordered-map-candidate.so`.

The shared seed at `examples/starters/unordered-map-rs` provides an editable
`src/lib.rs` with an intentionally naive Rust candidate using one mutex and
`HashMap`. Build and validate it from a materialized workspace:

    make
    go -C _evaluator/unordered-map run . check --workspace "$PWD" --scenario swmr
    go -C _evaluator/unordered-map run . benchmark --workspace "$PWD" --scenario swmr --duration 1s --warmup 0s

The starter is untrusted and may be replaced with any implementation that
exports the same ABI. The trusted checker requires linearizable concurrent put,
get, and remove. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
