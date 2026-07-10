# Queue SPSC Input

This input targets a single-producer, single-consumer bounded FIFO queue. The
manifest invokes the trusted Go evaluator directly. Candidates implement the
copying C ABI documented at `_evaluator/queue/CANDIDATE_CONTRACT.md` and
export it from `./queue-candidate.so`.

The shared seed at `examples/starters/queue-rs` provides an editable
`src/lib.rs` with an intentionally naive Rust candidate using one mutex and
`VecDeque`. Build and validate it from a materialized workspace:

    make
    go -C _evaluator/queue run . check --workspace "$PWD" --scenario spsc
    go -C _evaluator/queue run . benchmark --workspace "$PWD" --scenario spsc --duration 1s --warmup 0s

The starter is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
