# Stack SPSC Input

This input targets a single-producer, single-consumer bounded stack. The
manifest invokes the trusted Go evaluator directly. Candidates implement the
copying C ABI documented at `_evaluator/stack/CANDIDATE_CONTRACT.md` and export
it from `./stack-candidate.so`.

The shared seed at `examples/starters/stack-rs` provides an editable `src/lib.rs`
with an intentionally naive Rust candidate using one mutex and `Vec`. Build and
validate it from a materialized workspace:

    make
    go -C _evaluator/stack run . check --workspace "$PWD" --scenario spsc
    go -C _evaluator/stack run . benchmark --workspace "$PWD" --scenario spsc --duration 1s --warmup 0s

The starter is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
