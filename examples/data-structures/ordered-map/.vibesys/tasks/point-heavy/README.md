# Ordered Map Point-Heavy Task

This task targets a multi-writer ordered map whose timed mix is get, put, and
remove. The manifest invokes the `vibesys-evaluator-ordered-map` package.
Candidates implement the copying C ABI documented in the evaluator package's
`CANDIDATE_CONTRACT.md` (`resources/evaluators/ordered-map` in the VibeSys
repository) and export it from `./ordered-map-candidate.so`. Ordered neighbor
and range operations are still required by the correctness gate.

The project root provides an editable `src/lib.rs` with an intentionally naive
Rust candidate using one mutex and `BTreeMap`. Build and validate it from the
project root:

    make
    go -C <vibesys>/resources/evaluators/ordered-map run . check --workspace "$PWD" --scenario point-heavy
    go -C <vibesys>/resources/evaluators/ordered-map run . benchmark --workspace "$PWD" --scenario point-heavy --duration 1s --warmup 0s

The candidate is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The gate requires concurrent
linearizability of point operations. Ordered operations that overlap a mutation
are weakly consistent. The manifest benchmark uses three repetitions and reports
their median as `total_ops_per_sec`.
