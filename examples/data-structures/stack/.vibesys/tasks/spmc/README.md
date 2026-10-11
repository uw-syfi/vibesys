# Stack SPMC Task

This task targets a single-producer, multi-consumer bounded stack. The manifest
invokes the shared trusted stack evaluator and ABI.

The project root holds an intentionally naive Rust candidate. From the project
root, run:

    make
    go -C <vibesys>/resources/evaluators/stack run . check --workspace "$PWD" --scenario spmc
    go -C <vibesys>/resources/evaluators/stack run . benchmark --workspace "$PWD" --scenario spmc --duration 1s --warmup 0s

The candidate is untrusted and may be replaced by any implementation exporting
the ABI in the evaluator package's `CANDIDATE_CONTRACT.md`
(`resources/evaluators/stack` in the VibeSys repository).
