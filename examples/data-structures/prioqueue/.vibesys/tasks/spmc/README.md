# Priority Queue SPMC Task

This task targets a single-producer, multi-consumer bounded priority queue. The
manifest invokes the shared trusted priority queue evaluator and ABI.

The project root holds an intentionally naive Rust candidate. From the project
root, run:

    make
    go -C <vibesys>/resources/evaluators/priority-queue run . check --workspace "$PWD" --scenario spmc
    go -C <vibesys>/resources/evaluators/priority-queue run . benchmark --workspace "$PWD" --scenario spmc --duration 1s --warmup 0s

The candidate is untrusted and may be replaced by any implementation exporting
the ABI in the evaluator package's `CANDIDATE_CONTRACT.md`
(`resources/evaluators/priority-queue` in the VibeSys repository).
