# Stack SPMC Input

This input targets a single-producer, multi-consumer bounded stack. The
manifest invokes the shared trusted stack evaluator and ABI.

The shared seed at `examples/starters/stack-rs` is an intentionally naive Rust
candidate. From a materialized workspace, run:

    make
    go -C _evaluator/stack run . check --workspace "$PWD" --scenario spmc
    go -C _evaluator/stack run . benchmark --workspace "$PWD" --scenario spmc --duration 1s --warmup 0s

The starter is untrusted and may be replaced by any implementation exporting
the ABI in `_evaluator/stack/CANDIDATE_CONTRACT.md`.
