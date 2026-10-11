# Priority Queue SPSC Task

This task targets a single-producer, single-consumer bounded priority queue. The
manifest invokes the `vibesys-evaluator-priority-queue` package. Candidates
implement the copying C ABI documented in the evaluator package's
`CANDIDATE_CONTRACT.md` (`resources/evaluators/priority-queue` in the VibeSys
repository) and export it from `./priority-queue-candidate.so`.

The project root provides an editable `src/lib.rs` with an intentionally naive
Rust candidate using one mutex and `BinaryHeap`. Build and validate it from the
project root:

    make
    go -C <vibesys>/resources/evaluators/priority-queue run . check --workspace "$PWD" --scenario spsc
    go -C <vibesys>/resources/evaluators/priority-queue run . benchmark --workspace "$PWD" --scenario spsc --duration 1s --warmup 0s

The candidate is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
