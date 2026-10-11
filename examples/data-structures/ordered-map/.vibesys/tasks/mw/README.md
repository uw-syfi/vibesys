# Ordered Map MW Task

This task targets a multi-writer concurrent ordered map. The manifest invokes
the `vibesys-evaluator-ordered-map` package. Candidates implement the copying C
ABI documented in the evaluator package's `CANDIDATE_CONTRACT.md`
(`resources/evaluators/ordered-map` in the VibeSys repository) and export it
from `./ordered-map-candidate.so`. Ordered neighbor and range operations are
required.

The project root provides an editable `src/lib.rs` with an intentionally naive
Rust candidate using one mutex and `BTreeMap`. Build and validate it from the
project root:

    make
    go -C <vibesys>/resources/evaluators/ordered-map run . check --workspace "$PWD" --scenario mw
    go -C <vibesys>/resources/evaluators/ordered-map run . benchmark --workspace "$PWD" --scenario mw --duration 1s --warmup 0s

The candidate is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The gate requires concurrent
linearizability of point operations. Ordered operations that overlap a mutation
are weakly consistent. The manifest benchmark uses three repetitions and reports
their median as `total_ops_per_sec`.
