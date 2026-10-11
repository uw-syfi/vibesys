# Unordered Map SWMR Task

This task targets a single-writer, multi-reader concurrent unordered map. The
manifest invokes the `vibesys-evaluator-unordered-map` package. Candidates
implement the copying C ABI documented in the evaluator package's
`CANDIDATE_CONTRACT.md` (`resources/evaluators/unordered-map` in the VibeSys
repository) and export it from `./unordered-map-candidate.so`.

The project root provides an editable `src/lib.rs` with an intentionally naive
Rust candidate using one mutex and `HashMap`. Build and validate it from the
project root:

    make
    go -C <vibesys>/resources/evaluators/unordered-map run . check --workspace "$PWD" --scenario swmr
    go -C <vibesys>/resources/evaluators/unordered-map run . benchmark --workspace "$PWD" --scenario swmr --duration 1s --warmup 0s

The candidate is untrusted and may be replaced with any implementation that
exports the same ABI. The trusted checker requires linearizable concurrent put,
get, and remove. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
