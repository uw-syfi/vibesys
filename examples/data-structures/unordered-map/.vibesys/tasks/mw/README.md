# Unordered Map MW Task

This task targets a mixed multi-writer concurrent unordered map. The manifest
invokes the shared trusted unordered map evaluator and ABI.

The project root holds an intentionally naive Rust candidate. From the project
root, run:

    make
    go -C <vibesys>/resources/evaluators/unordered-map run . check --workspace "$PWD" --scenario mw
    go -C <vibesys>/resources/evaluators/unordered-map run . benchmark --workspace "$PWD" --scenario mw --duration 1s --warmup 0s

The candidate is untrusted and may be replaced by any implementation exporting
the ABI in the evaluator package's `CANDIDATE_CONTRACT.md`
(`resources/evaluators/unordered-map` in the VibeSys repository). The trusted
checker requires linearizable concurrent put, get, and remove.
