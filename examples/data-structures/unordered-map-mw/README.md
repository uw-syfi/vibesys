# Unordered Map MW Input

This input targets a mixed multi-writer concurrent unordered map. The
manifest invokes the shared trusted unordered map evaluator and ABI.

The shared seed at `examples/starters/unordered-map-rs` is an intentionally
naive Rust candidate. From a materialized workspace, run:

    make
    go -C _evaluator/unordered-map run . check --workspace "$PWD" --scenario mw
    go -C _evaluator/unordered-map run . benchmark --workspace "$PWD" --scenario mw --duration 1s --warmup 0s

The starter is untrusted and may be replaced by any implementation exporting
the ABI in `_evaluator/unordered-map/CANDIDATE_CONTRACT.md`. The trusted checker
requires linearizable concurrent put, get, and remove.
